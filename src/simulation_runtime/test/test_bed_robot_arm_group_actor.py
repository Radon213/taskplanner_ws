from __future__ import annotations

from pathlib import Path

from procedure_spec import load_bundle
from simulation_runtime.llm_surgeon_actor import LLMSurgeonActorNode
from surgical_msgs.msg import BedRobotArmGroupStatus


def _actor(procedure: str = "thyroidectomy", phase_id: str = "P04"):
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / procedure
    )
    actor = LLMSurgeonActorNode.__new__(LLMSurgeonActorNode)
    actor._spec = load_bundle(spec_dir)
    actor._current_phase_id = phase_id
    actor._pending_action = ""
    actor._pending_group_requests = {}
    actor._bed_group_status_stamp_ns = {}
    actor._bed_group_states = {
        "suction": {
            "connected": True,
            "state": "standby",
            "end_effector_profile": "suction",
        },
        "retraction": {
            "connected": True,
            "state": "holding",
            "end_effector_profile": "thyroid_retractor",
        },
    }
    return actor


def _operations(actor, group_id: str) -> set[str]:
    return {
        cue["operation"]
        for cue in actor._available_bed_robot_arm_group_cues()
        if cue["group_id"] == group_id
    }


def test_suction_cues_follow_group_state_and_can_repeat_after_completion():
    actor = _actor()
    assert "suction_start" in _operations(actor, "suction")
    assert "suction_stop" not in _operations(actor, "suction")

    actor._pending_group_requests["suction"] = {"request_id": "pending"}
    assert not _operations(actor, "suction")
    actor._pending_group_requests.clear()
    assert "suction_start" in _operations(actor, "suction")

    actor._bed_group_states["suction"]["state"] = "suctioning"
    assert "suction_stop" in _operations(actor, "suction")
    assert "suction_start" not in _operations(actor, "suction")


def test_humanoid_pending_masks_tool_work_but_not_independent_group_cue():
    actor = _actor()
    actor._pending_action = "handover"
    group_decision = {
        "action": "request_bed_robot_arm_group",
        "group_id": "suction",
        "group_operation": "suction_start",
    }
    assert actor._mask_decision_for_pending_lanes(group_decision) == group_decision

    tool_decision = {
        "action": "request_tool",
        "tool": "T01",
        "phase": "P04",
    }
    assert actor._mask_decision_for_pending_lanes(tool_decision)["action"] == "wait"


def test_pending_group_blocks_phase_advance_but_not_humanoid_tool_lane():
    actor = _actor()
    actor._pending_group_requests["retraction"] = {"request_id": "pending"}
    advance = {"action": "advance_phase", "phase": "P05"}
    assert actor._mask_decision_for_pending_lanes(advance)["action"] == "wait"

    tool_decision = {"action": "request_tool", "tool": "T01", "phase": "P04"}
    assert actor._mask_decision_for_pending_lanes(tool_decision) == tool_decision


def test_inguinal_exchange_has_no_release_first_dead_end():
    actor = _actor("inguinal_hernia_repair", "P04")
    actor._bed_group_states["retraction"] = {
        "connected": True,
        "state": "holding",
        "end_effector_profile": "army_navy",
    }
    cues = actor._available_bed_robot_arm_group_cues()
    assert any(cue["operation"] == "change_end_effector" for cue in cues)
    assert not any(cue["operation"] == "release_retraction" for cue in cues)


def test_actor_ignores_status_older_than_current_group_state():
    actor = _actor()
    actor._record_event = lambda *args, **kwargs: None
    actor._schedule_next_decision = lambda *_args, **_kwargs: None

    completed = BedRobotArmGroupStatus()
    completed.stamp.sec = 20
    completed.request_id = "req-start"
    completed.group_id = "suction"
    completed.operation = "suction_start"
    completed.state = "suctioning"
    completed.terminal = True
    completed.success = True
    actor._on_bed_robot_arm_group_status(completed)

    delayed = BedRobotArmGroupStatus()
    delayed.stamp.sec = 10
    delayed.request_id = "req-stop"
    delayed.group_id = "suction"
    delayed.operation = "suction_stop"
    delayed.state = "standby"
    delayed.terminal = True
    delayed.success = False
    actor._on_bed_robot_arm_group_status(delayed)

    assert actor._bed_group_states["suction"]["state"] == "suctioning"
    assert actor._bed_group_states["suction"]["operation"] == "suction_start"


def test_actor_health_heartbeat_preserves_holding_metadata():
    actor = _actor()
    actor._bed_group_states["retraction"].update(
        {
            "operation": "retraction",
            "direction": "LEFT_RIGHT",
            "distance_mm": 10.0,
            "distance_origin": "qualitative_inferred",
            "error_code": "distance_limit_exceeded",
            "rejection_reason": "50 mm exceeds the configured controller limit",
        }
    )

    ready = BedRobotArmGroupStatus()
    ready.stamp.sec = 30
    ready.request_id = "health-retraction"
    ready.group_id = "retraction"
    ready.state = "holding"
    ready.terminal = True
    ready.success = True
    actor._on_bed_robot_arm_group_status(ready)

    state = actor._bed_group_states["retraction"]
    assert state["state"] == "holding"
    assert state["operation"] == "retraction"
    assert state["direction"] == "LEFT_RIGHT"
    assert state["distance_mm"] == 10.0
    assert state["end_effector_profile"] == "thyroid_retractor"
    assert state["error_code"] == "distance_limit_exceeded"
    assert state["rejection_reason"] == "50 mm exceeds the configured controller limit"
