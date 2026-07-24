from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bt_orchestrator.bed_robot_arm_group_orchestrator import (
    BedRobotArmGroupOrchestrator,
    PendingRetraction,
)
from procedure_spec import load_bundle
from surgical_msgs.msg import (
    BedRobotArmGroupCommand,
    BedRobotArmGroupRequest,
    BedRobotArmGroupState,
    BedRobotArmGroupStatus,
    WorldState,
)


def _router_at_thyroid_phase(phase_id: str):
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )
    router = BedRobotArmGroupOrchestrator.__new__(BedRobotArmGroupOrchestrator)
    router._spec = load_bundle(spec_dir)
    router._bt_ready = True
    router._world = SimpleNamespace(filtered_phase=phase_id)
    router._group_states = {}
    return router


def test_suction_stop_tokens_take_priority_over_short_scenario_start_cue():
    router = _router_at_thyroid_phase("P04")
    for utterance in ("석션 빼", "석션 빠져", "석션 스탑"):
        assert router._classify_voice(utterance) == (
            "suction",
            "suction_stop",
            "suction",
        )


def test_army_name_inside_retraction_does_not_trigger_profile_change():
    router = _router_at_thyroid_phase("P04")
    assert router._classify_voice("아미를 위로 조금 당겨줘") == (
        "retraction",
        "retraction",
        "army",
    )


def test_explicit_army_exchange_still_routes_profile_change():
    router = _router_at_thyroid_phase("P04")
    assert router._classify_voice("리트랙터를 아미로 교환해줘") == (
        "retraction",
        "change_end_effector",
        "army",
    )


def _guard_router():
    router = BedRobotArmGroupOrchestrator.__new__(BedRobotArmGroupOrchestrator)
    router._request_guard_reason = lambda _request: ""
    router._confidence_threshold = 0.6
    router._visual_direction_threshold = 0.75
    router._group_states = {
        "retraction": SimpleNamespace(
            connected=True,
            state="holding",
            end_effector_profile="thyroid_retractor",
        )
    }
    return router


def _proposal(**overrides):
    values = {
        "group_id": "retraction",
        "operation": "retraction",
        "direction": "RIGHT",
        "confidence": 0.95,
        "rationale": "spoken direction",
        "raw_distance_text": "10 mm",
        "distance_mm": 10.0,
        "distance_origin": "explicit_with_unit",
        "end_effector_profile": "thyroid_retractor",
    }
    values.update(overrides)
    return SimpleNamespace(
        valid=True,
        validation_error="",
        command=SimpleNamespace(**values),
    )


def test_bt_revalidates_distance_against_full_original_utterance():
    request = SimpleNamespace(
        voice_text="오른쪽으로 50 mm 당겨줘",
        end_effector_profile="thyroid_retractor",
    )
    proposal = _proposal(
        raw_distance_text="",
        distance_mm=10.0,
        distance_origin="defaulted",
    )
    reason = _guard_router()._proposal_guard_reason(request, proposal)
    assert "original request" in reason


def test_bt_rejects_profile_injected_by_vlm_when_request_omits_profile():
    request = SimpleNamespace(
        voice_text="오른쪽으로 10 mm 당겨줘",
        end_effector_profile="",
    )
    reason = _guard_router()._proposal_guard_reason(
        request,
        _proposal(end_effector_profile="army"),
    )
    assert "active group profile" in reason


def test_newer_world_snapshot_can_seed_profile_after_health_heartbeat():
    router = BedRobotArmGroupOrchestrator.__new__(BedRobotArmGroupOrchestrator)
    health = BedRobotArmGroupState()
    health.group_id = "retraction"
    health.connected = True
    health.state = "standby"
    health.stamp.sec = 10
    router._group_states = {"retraction": health}

    seeded = BedRobotArmGroupState()
    seeded.group_id = "retraction"
    seeded.connected = True
    seeded.state = "standby"
    seeded.end_effector_profile = "thyroid_retractor"
    seeded.stamp.sec = 11
    world = WorldState()
    world.bed_robot_arm_groups = [seeded]
    router._on_world(world)
    assert router._group_states["retraction"].end_effector_profile == "thyroid_retractor"

    stale = BedRobotArmGroupState()
    stale.group_id = "retraction"
    stale.end_effector_profile = "stale_profile"
    stale.stamp.sec = 9
    world.bed_robot_arm_groups = [stale]
    router._on_world(world)
    assert router._group_states["retraction"].end_effector_profile == "thyroid_retractor"


def test_stale_world_snapshot_cannot_erase_active_direct_status():
    router = BedRobotArmGroupOrchestrator.__new__(BedRobotArmGroupOrchestrator)
    active = BedRobotArmGroupState()
    active.group_id = "suction"
    active.connected = True
    active.state = "suctioning"
    active.operation = "suction_start"
    active.active_request_id = "req-active"
    active.active_command_id = "cmd-active"
    active.stamp.sec = 20
    router._group_states = {"suction": active}

    stale = BedRobotArmGroupState()
    stale.group_id = "suction"
    stale.connected = True
    stale.state = "standby"
    stale.stamp.sec = 10
    world = WorldState()
    world.bed_robot_arm_groups = [stale]
    router._on_world(world)

    observed = router._group_states["suction"]
    assert observed.state == "suctioning"
    assert observed.active_request_id == "req-active"


def test_stale_world_snapshot_cannot_rollback_terminal_profile():
    router = BedRobotArmGroupOrchestrator.__new__(BedRobotArmGroupOrchestrator)
    completed = BedRobotArmGroupState()
    completed.group_id = "retraction"
    completed.connected = True
    completed.state = "standby"
    completed.end_effector_profile = "army"
    completed.stamp.sec = 30
    router._group_states = {"retraction": completed}

    stale = BedRobotArmGroupState()
    stale.group_id = "retraction"
    stale.connected = True
    stale.state = "standby"
    stale.end_effector_profile = "thyroid_retractor"
    stale.stamp.sec = 25
    world = WorldState()
    world.bed_robot_arm_groups = [stale]
    router._on_world(world)

    assert router._group_states["retraction"].end_effector_profile == "army"


def test_older_terminal_status_cannot_rollback_newer_terminal_state():
    router = BedRobotArmGroupOrchestrator.__new__(BedRobotArmGroupOrchestrator)
    current = BedRobotArmGroupState()
    current.group_id = "suction"
    current.connected = True
    current.state = "suctioning"
    current.operation = "suction_start"
    current.stamp.sec = 20
    router._group_states = {"suction": current}
    router._inflight_commands = {}
    router._pending_retraction = None

    delayed = BedRobotArmGroupStatus()
    delayed.stamp.sec = 10
    delayed.request_id = "req-stop"
    delayed.group_id = "suction"
    delayed.operation = "suction_stop"
    delayed.state = "standby"
    delayed.terminal = True
    delayed.error_code = "request_in_flight"
    router._on_group_status(delayed)

    assert router._group_states["suction"].state == "suctioning"
    assert router._group_states["suction"].operation == "suction_start"


def test_health_heartbeat_preserves_retraction_operation_metadata():
    router = BedRobotArmGroupOrchestrator.__new__(BedRobotArmGroupOrchestrator)
    current = BedRobotArmGroupState()
    current.group_id = "retraction"
    current.connected = True
    current.state = "holding"
    current.operation = "retraction"
    current.direction = "UP_DOWN"
    current.distance_mm = 10.0
    current.distance_origin = "explicit_unit_inferred"
    current.end_effector_profile = "thyroid_retractor"
    current.error_code = "distance_limit_exceeded"
    current.error_message = "controller rejected 50 mm"
    current.rejection_reason = "50 mm exceeds the configured controller limit"
    current.stamp.sec = 20
    router._group_states = {"retraction": current}
    router._inflight_commands = {}
    router._pending_retraction = None

    ready = BedRobotArmGroupStatus()
    ready.stamp.sec = 30
    ready.request_id = "health-retraction"
    ready.group_id = "retraction"
    ready.state = "holding"
    ready.terminal = True
    ready.success = True
    ready.outcome = "available"
    router._on_group_status(ready)

    observed = router._group_states["retraction"]
    assert observed.state == "holding"
    assert observed.operation == "retraction"
    assert observed.direction == "UP_DOWN"
    assert observed.distance_mm == 10.0
    assert observed.end_effector_profile == "thyroid_retractor"
    assert observed.error_code == "distance_limit_exceeded"
    assert observed.error_message == "controller rejected 50 mm"
    assert observed.rejection_reason == "50 mm exceeds the configured controller limit"


def _nephrectomy_change_guard(target_profile: str) -> str:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "nephrectomy"
    )
    router = BedRobotArmGroupOrchestrator.__new__(BedRobotArmGroupOrchestrator)
    router._spec = load_bundle(spec_dir)
    router._bt_ready = True
    router._world = SimpleNamespace(
        running=True,
        execution_state="running",
        procedure_id="nephrectomy",
        filtered_phase="P02",
    )
    router._group_states = {
        "retraction": SimpleNamespace(
            connected=True,
            state="holding",
            operation="retraction",
            active_request_id="",
            active_command_id="",
            end_effector_profile="mayo",
        )
    }
    request = SimpleNamespace(
        request_id="change-request",
        group_id="retraction",
        operation="change_end_effector",
        procedure_id="nephrectomy",
        phase_id="P02",
        end_effector_profile=target_profile,
    )
    return router._request_guard_reason(request)


def test_bt_allows_only_phase_declared_end_effector_transition():
    assert _nephrectomy_change_guard("malleable") == ""
    assert "not allowed" in _nephrectomy_change_guard("army")


class _CapturePublisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, message) -> None:
        self.messages.append(message)


def _ordering_router(state: str = "standby"):
    router = BedRobotArmGroupOrchestrator.__new__(BedRobotArmGroupOrchestrator)
    router._request_guard_reason = lambda _request: ""
    router._group_states = {
        "suction": SimpleNamespace(
            state=state,
            operation="",
            active_request_id="",
            active_command_id="",
            end_effector_profile="suction",
        ),
        "retraction": SimpleNamespace(
            state=state,
            operation="",
            active_request_id="",
            active_command_id="",
            end_effector_profile="thyroid_retractor",
        ),
    }
    router._pending_retraction = None
    router._inflight_commands = {}
    router._seen_request_ids = set()
    router._dispatched_request_ids = set()
    router._recent_voice_requests = {}
    router._command_pub = _CapturePublisher()
    router._status_pub = _CapturePublisher()
    router._stamp = lambda: BedRobotArmGroupStatus().stamp
    return router


def _request(request_id: str, group_id: str, operation: str, voice_text: str):
    request = BedRobotArmGroupRequest()
    request.request_id = request_id
    request.group_id = group_id
    request.operation = operation
    request.voice_text = voice_text
    request.end_effector_profile = (
        "thyroid_retractor" if group_id == "retraction" else "suction"
    )
    return request


def test_local_lane_slot_prevents_stop_from_overtaking_suction_start():
    router = _ordering_router("standby")
    router._on_group_request(
        _request("req-start", "suction", "suction_start", "석션 시작")
    )
    router._on_group_request(
        _request("req-stop", "suction", "suction_stop", "석션 스탑")
    )

    assert [item.operation for item in router._command_pub.messages] == ["suction_start"]
    assert router._inflight_commands["suction"].request_id == "req-start"
    rejected = router._status_pub.messages[-1]
    assert rejected.request_id == "req-stop"
    assert rejected.error_code == "request_in_flight"


def test_release_cancels_pending_vlm_request_before_dispatch():
    router = _ordering_router("holding")
    pending = _request(
        "req-retract", "retraction", "retraction", "왼쪽으로 10 mm 당겨줘"
    )
    router._pending_retraction = PendingRetraction(pending, 0.0)

    router._on_group_request(
        _request("req-release", "retraction", "release_retraction", "견인 해제")
    )

    assert router._pending_retraction is None
    assert [item.operation for item in router._command_pub.messages] == [
        "release_retraction"
    ]
    cancelled = router._status_pub.messages[0]
    assert cancelled.request_id == "req-retract"
    assert cancelled.outcome == "cancelled_by_newer_request"
    assert router._inflight_commands["retraction"].request_id == "req-release"


def test_second_incremental_retraction_is_rejected_while_lane_is_in_flight():
    router = _ordering_router("holding")
    active = BedRobotArmGroupCommand()
    active.request_id = "req-first"
    active.command_id = "cmd-first"
    active.group_id = "retraction"
    active.operation = "retraction"
    active.direction = "LEFT"
    active.distance_mm = 10.0
    router._inflight_commands["retraction"] = active

    router._on_group_request(
        _request(
            "req-second",
            "retraction",
            "retraction",
            "오른쪽으로 10 더 당겨줘",
        )
    )

    assert router._command_pub.messages == []
    rejected = router._status_pub.messages[-1]
    assert rejected.request_id == "req-second"
    assert rejected.success is False
    assert rejected.error_code == "request_in_flight"
