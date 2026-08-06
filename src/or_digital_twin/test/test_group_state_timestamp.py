from __future__ import annotations

from pathlib import Path

import pytest

from or_digital_twin.node import ORDigitalTwinNode
from or_digital_twin.models import (
    LIFECYCLE_MAYO_REUSE,
    LIFECYCLE_PREPOSITIONED_RIGHT,
    LIFECYCLE_SURGEON_OWNED,
)
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from std_msgs.msg import String
from surgical_msgs.msg import (
    BedRobotArmGroupCommand,
    BedRobotArmGroupStatus,
    SurgeonRequest,
    TwinEvent,
)


def _thyroid_spec():
    return load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )


def _thyroid_demo_spec():
    return load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )


def test_group_reducer_preserves_source_status_timestamp_in_payload():
    twin = ORDigitalTwin(_thyroid_spec())
    status = BedRobotArmGroupStatus()
    status.stamp.sec = 123
    status.stamp.nanosec = 456
    status.request_id = "req-1"
    status.command_id = "cmd-1"
    status.group_id = "suction"
    status.operation = "suction_start"
    status.state = "suctioning"
    twin.update_bed_robot_arm_group_status(status)

    payload = next(
        item for item in twin.bed_robot_arm_group_payload() if item["group_id"] == "suction"
    )
    assert payload["last_update_stamp_sec"] == 123
    assert payload["last_update_stamp_nanosec"] == 456


def test_periodic_world_serialization_does_not_retimestamp_unchanged_group():
    payload = {
        "group_id": "retraction",
        "connected": True,
        "state": "holding",
        "end_effector_profile": "army",
        "last_update_stamp_sec": 12,
        "last_update_stamp_nanosec": 34,
    }

    class SnapshotStamp:
        sec = 999
        nanosec = 888

    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    first = node._bed_robot_arm_group_state_message(payload, SnapshotStamp())
    second = node._bed_robot_arm_group_state_message(payload, SnapshotStamp())

    assert (first.stamp.sec, first.stamp.nanosec) == (12, 34)
    assert (second.stamp.sec, second.stamp.nanosec) == (12, 34)


def test_late_command_callback_cannot_resurrect_terminal_group_state():
    twin = ORDigitalTwin(_thyroid_spec())
    terminal = BedRobotArmGroupStatus()
    terminal.stamp.sec = 20
    terminal.request_id = "req-complete"
    terminal.command_id = "cmd-complete"
    terminal.group_id = "retraction"
    terminal.operation = "retraction"
    terminal.state = "holding"
    terminal.terminal = True
    terminal.success = True
    terminal.end_effector_profile = "thyroid_retractor"
    twin.update_bed_robot_arm_group_status(terminal)

    late_command = BedRobotArmGroupCommand()
    late_command.stamp.sec = 10
    late_command.request_id = "req-complete"
    late_command.command_id = "cmd-complete"
    late_command.group_id = "retraction"
    late_command.operation = "retraction"
    late_command.direction = "LEFT"
    late_command.distance_mm = 10.0

    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = twin
    node._publish_event = lambda *args, **kwargs: None
    node._publish_world_state = lambda: None
    node._on_bed_robot_arm_group_command(late_command)

    belief = twin.state.bed_robot_arm_groups["retraction"]
    assert belief.state == "holding"
    assert belief.active_request_id == ""
    assert belief.active_command_id == ""
    assert belief.last_update_stamp_sec == 20


def test_older_terminal_status_cannot_rollback_newer_terminal_state():
    twin = ORDigitalTwin(_thyroid_spec())
    completed = BedRobotArmGroupStatus()
    completed.stamp.sec = 20
    completed.request_id = "req-start"
    completed.command_id = "cmd-start"
    completed.group_id = "suction"
    completed.operation = "suction_start"
    completed.state = "suctioning"
    completed.terminal = True
    completed.success = True
    twin.update_bed_robot_arm_group_status(completed)

    delayed_rejection = BedRobotArmGroupStatus()
    delayed_rejection.stamp.sec = 10
    delayed_rejection.request_id = "req-stop"
    delayed_rejection.group_id = "suction"
    delayed_rejection.operation = "suction_stop"
    delayed_rejection.state = "standby"
    delayed_rejection.terminal = True
    delayed_rejection.success = False
    delayed_rejection.error_code = "request_in_flight"
    twin.update_bed_robot_arm_group_status(delayed_rejection)

    belief = twin.state.bed_robot_arm_groups["suction"]
    assert belief.state == "suctioning"
    assert belief.operation == "suction_start"
    assert belief.last_update_stamp_sec == 20


def test_health_heartbeat_preserves_operation_and_restores_state_after_reconnect():
    twin = ORDigitalTwin(_thyroid_spec())
    completed = BedRobotArmGroupStatus()
    completed.stamp.sec = 20
    completed.request_id = "req-retract"
    completed.command_id = "cmd-retract"
    completed.group_id = "retraction"
    completed.operation = "retraction"
    completed.state = "holding"
    completed.direction = "LEFT_RIGHT"
    completed.distance_mm = 10.0
    completed.distance_origin = "qualitative_inferred"
    completed.end_effector_profile = "thyroid_retractor"
    completed.terminal = True
    completed.success = False
    completed.error_code = "distance_limit_exceeded"
    completed.rejection_reason = "50 mm exceeds the configured controller limit"
    twin.update_bed_robot_arm_group_status(completed)

    ready = BedRobotArmGroupStatus()
    ready.stamp.sec = 30
    ready.request_id = "health-retraction"
    ready.group_id = "retraction"
    ready.state = "holding"
    ready.terminal = True
    ready.success = True
    ready.outcome = "available"
    twin.update_bed_robot_arm_group_status(ready)

    belief = twin.state.bed_robot_arm_groups["retraction"]
    assert belief.state == "holding"
    assert belief.operation == "retraction"
    assert belief.direction == "LEFT_RIGHT"
    assert belief.distance_mm == 10.0
    assert belief.end_effector_profile == "thyroid_retractor"
    assert belief.error_code == "distance_limit_exceeded"
    assert belief.rejection_reason == "50 mm exceeds the configured controller limit"

    offline = BedRobotArmGroupStatus()
    offline.stamp.sec = 40
    offline.request_id = "health-retraction"
    offline.group_id = "retraction"
    offline.state = "offline"
    offline.terminal = True
    offline.success = False
    offline.error_code = "server_unavailable"
    twin.update_bed_robot_arm_group_status(offline)
    assert belief.state == "offline"
    assert belief.connected is False

    ready.stamp.sec = 50
    twin.update_bed_robot_arm_group_status(ready)
    assert belief.state == "holding"
    assert belief.connected is True
    assert belief.operation == "retraction"
    assert belief.direction == "LEFT_RIGHT"
    assert belief.distance_mm == 10.0
    assert belief.error_code == "distance_limit_exceeded"
    assert belief.rejection_reason == "50 mm exceeds the configured controller limit"


def test_repeated_health_heartbeat_only_records_availability_transitions():
    twin = ORDigitalTwin(_thyroid_spec())
    offline = BedRobotArmGroupStatus()
    offline.stamp.sec = 10
    offline.request_id = "health-retraction"
    offline.group_id = "retraction"
    offline.state = "offline"
    offline.terminal = True
    offline.success = False
    offline.error_code = "server_unavailable"
    offline.outcome = "server_unavailable"

    assert twin.update_bed_robot_arm_group_status(offline) is True
    first_count = len(twin.event_history)

    offline.stamp.sec = 11
    assert twin.update_bed_robot_arm_group_status(offline) is False
    assert len(twin.event_history) == first_count

    ready = BedRobotArmGroupStatus()
    ready.stamp.sec = 12
    ready.request_id = "health-retraction"
    ready.group_id = "retraction"
    ready.state = "standby"
    ready.terminal = True
    ready.success = True
    ready.outcome = "available"

    assert twin.update_bed_robot_arm_group_status(ready) is True
    assert len(twin.event_history) == first_count + 1

    ready.stamp.sec = 13
    assert twin.update_bed_robot_arm_group_status(ready) is False
    assert len(twin.event_history) == first_count + 1


def test_node_does_not_publish_unchanged_health_heartbeat_as_event():
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = ORDigitalTwin(_thyroid_spec())
    node._pending_bed_robot_arm_group_requests = {}
    published_events = []
    published_world_states = []
    node._publish_event = lambda event_type, **kwargs: published_events.append(
        (event_type, kwargs)
    )
    node._publish_world_state = lambda: published_world_states.append(True)

    ready = BedRobotArmGroupStatus()
    ready.stamp.sec = 10
    ready.request_id = "health-retraction"
    ready.group_id = "retraction"
    ready.state = "standby"
    ready.terminal = True
    ready.success = True
    ready.outcome = "available"

    node._on_bed_robot_arm_group_status(ready)

    assert published_events == []
    assert published_world_states == []

    offline = BedRobotArmGroupStatus()
    offline.stamp.sec = 11
    offline.request_id = "health-retraction"
    offline.group_id = "retraction"
    offline.state = "offline"
    offline.terminal = True
    offline.success = False
    offline.error_code = "server_unavailable"
    offline.outcome = "server_unavailable"

    node._on_bed_robot_arm_group_status(offline)

    assert [event_type for event_type, _ in published_events] == [
        "BedRobotArmGroupAvailabilityChanged"
    ]
    assert published_world_states == [True]


def test_group_reducer_suppresses_duplicate_progress_but_keeps_semantic_boundaries():
    twin = ORDigitalTwin(_thyroid_spec())

    def status(
        *,
        stamp: int,
        state: str,
        outcome: str,
        progress: float,
        terminal: bool = False,
    ) -> BedRobotArmGroupStatus:
        msg = BedRobotArmGroupStatus()
        msg.stamp.sec = stamp
        msg.request_id = "req-1"
        msg.command_id = "cmd-1"
        msg.group_id = "suction"
        msg.operation = "suction_start"
        msg.state = state
        msg.outcome = outcome
        msg.progress = progress
        msg.terminal = terminal
        msg.success = True
        return msg

    assert twin.update_bed_robot_arm_group_status(
        status(stamp=10, state="suctioning", outcome="executing", progress=0.05)
    ) is True
    first_count = len(twin.event_history)

    assert twin.update_bed_robot_arm_group_status(
        status(stamp=11, state="suctioning", outcome="executing", progress=0.20)
    ) is False
    assert len(twin.event_history) == first_count

    assert twin.update_bed_robot_arm_group_status(
        status(stamp=12, state="suctioning", outcome="executing", progress=0.25)
    ) is True
    assert twin.update_bed_robot_arm_group_status(
        status(stamp=13, state="stopping", outcome="executing", progress=0.25)
    ) is True
    assert twin.update_bed_robot_arm_group_status(
        status(
            stamp=14,
            state="suctioning",
            outcome="completed",
            progress=1.0,
            terminal=True,
        )
    ) is True
    assert [event["event_type"] for event in list(twin.event_history)[-4:]] == [
        "BedRobotArmGroupStatusUpdated",
        "BedRobotArmGroupStatusUpdated",
        "BedRobotArmGroupStatusUpdated",
        "BedRobotArmGroupCommandCompleted",
    ]


def test_ignored_stale_group_status_does_not_poison_duplicate_cache():
    twin = ORDigitalTwin(_thyroid_spec())

    def status(*, stamp: int, state: str) -> BedRobotArmGroupStatus:
        msg = BedRobotArmGroupStatus()
        msg.stamp.sec = stamp
        msg.request_id = "req-1"
        msg.command_id = "cmd-1"
        msg.group_id = "suction"
        msg.operation = "suction_start"
        msg.state = state
        msg.outcome = "executing"
        msg.progress = 0.25
        msg.success = True
        return msg

    assert twin.update_bed_robot_arm_group_status(
        status(stamp=20, state="suctioning")
    ) is True
    assert twin.update_bed_robot_arm_group_status(
        status(stamp=10, state="stopping")
    ) is None
    ignored_count = len(twin.event_history)
    assert twin.update_bed_robot_arm_group_status(
        status(stamp=11, state="stopping")
    ) is False
    assert len(twin.event_history) == ignored_count
    assert twin.update_bed_robot_arm_group_status(
        status(stamp=30, state="stopping")
    ) is True


def test_node_does_not_publish_rejected_group_status_as_a_state_update():
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = ORDigitalTwin(_thyroid_spec())
    node._pending_bed_robot_arm_group_requests = {}
    published_events = []
    published_world_states = []
    node._publish_event = lambda event_type, **kwargs: published_events.append(
        (event_type, kwargs)
    )
    node._publish_world_state = lambda: published_world_states.append(True)

    current = BedRobotArmGroupStatus()
    current.stamp.sec = 20
    current.request_id = "req-1"
    current.command_id = "cmd-1"
    current.group_id = "suction"
    current.operation = "suction_start"
    current.state = "suctioning"
    current.outcome = "executing"
    current.success = True
    node._on_bed_robot_arm_group_status(current)

    stale = BedRobotArmGroupStatus()
    stale.stamp.sec = 10
    stale.request_id = "req-1"
    stale.command_id = "cmd-1"
    stale.group_id = "suction"
    stale.operation = "suction_start"
    stale.state = "standby"
    stale.outcome = "executing"
    stale.success = True
    node._on_bed_robot_arm_group_status(stale)

    assert [event_type for event_type, _ in published_events] == [
        "BedRobotArmGroupStatusUpdated"
    ]
    assert published_world_states == [True]


def test_node_suppresses_duplicate_group_progress_event_and_world_publish():
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = ORDigitalTwin(_thyroid_spec())
    node._pending_bed_robot_arm_group_requests = {}
    published_events = []
    published_world_states = []
    node._publish_event = lambda event_type, **kwargs: published_events.append(
        (event_type, kwargs)
    )
    node._publish_world_state = lambda: published_world_states.append(True)

    status = BedRobotArmGroupStatus()
    status.stamp.sec = 10
    status.request_id = "req-1"
    status.command_id = "cmd-1"
    status.group_id = "suction"
    status.operation = "suction_start"
    status.state = "suctioning"
    status.outcome = "executing"
    status.progress = 0.05
    status.success = True

    node._on_bed_robot_arm_group_status(status)
    status.stamp.sec = 11
    status.progress = 0.20
    node._on_bed_robot_arm_group_status(status)

    assert [event_type for event_type, _ in published_events] == [
        "BedRobotArmGroupStatusUpdated"
    ]
    assert published_world_states == [True]


def test_voice_transcript_resolves_longest_tool_name_and_coalesces_duplicate_request():
    twin = ORDigitalTwin(_thyroid_spec())

    resolved = twin.update_explicit_request("Army navy retractor please")

    assert resolved == "T05"
    assert twin.state.explicit_request_tool == "T05"
    assert len(twin.state.surgeon_request_queue) == 1
    first_generation = twin.state.surgeon_request_generation
    assert first_generation > 0
    assert twin.explicit_request_voice_backed() is True

    structured = SurgeonRequest()
    structured.event_type = "request_tool"
    structured.requested_tool = "T05"
    structured.voice_text = "Army navy retractor please"
    structured.ready_for_handover = True
    twin.update_surgeon_request(structured)

    assert len(twin.state.surgeon_request_queue) == 1
    assert twin.state.surgeon_request_generation == first_generation


def test_new_identical_request_gets_a_new_generation_after_dequeue():
    twin = ORDigitalTwin(_thyroid_spec())

    assert twin.update_explicit_request("Bovie") == "T04"
    first_generation = twin.state.surgeon_request_generation
    twin._dequeue_active_request("test_completion")

    assert twin.update_explicit_request("Bovie") == "T04"
    assert twin.state.surgeon_request_generation > first_generation
    assert twin.request_queue_summary()["queued_generations"] == [
        twin.state.surgeon_request_generation
    ]


def test_voice_transcript_resolves_short_distinctive_tool_names():
    twin = ORDigitalTwin(_thyroid_spec())

    assert twin.update_explicit_request("Adson") == "T02"
    assert twin.update_explicit_request("Bovie") == "T04"
    assert twin.update_explicit_request("bipolar") == "T07"


def test_voice_correction_prefers_the_last_named_tool():
    twin = ORDigitalTwin(_thyroid_spec())

    resolved = twin.update_explicit_request("bipolar 아니야 Bovie 다시 줘")

    assert resolved == "T04"
    assert twin.state.explicit_request_tool == "T04"


def test_short_spoken_voice_correction_prefers_the_last_named_tool():
    twin = ORDigitalTwin(_thyroid_spec())

    resolved = twin.update_explicit_request("Bovie 아니 bipolar")

    assert resolved == "T07"
    assert twin.state.explicit_request_tool == "T07"


def test_production_default_uses_real_additional_tool_instance():
    twin = ORDigitalTwin(_thyroid_demo_spec())
    first_state = twin.instrument_states["T02#1"]
    second_state = twin.instrument_states["T02#2"]
    twin._set_lifecycle(
        first_state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )

    assert twin.update_explicit_request("Adson 하나 더") == "T02"
    assert twin.state.surgeon_request_instance_id == second_state.instance_id
    assert twin.handover_allowed() is True
    assert twin.drain_shadow_assumption_audit() == []


def test_legacy_shadow_completion_resolves_real_additional_instance():
    twin = ORDigitalTwin(_thyroid_demo_spec())
    first_state = twin.instrument_states["T02#1"]
    second_state = twin.instrument_states["T02#2"]
    twin._set_lifecycle(
        first_state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )

    assert twin.update_explicit_request("Adson 하나 더") == "T02"
    assert twin.handover_allowed() is True
    first_generation = twin.state.surgeon_request_generation
    assert twin.state.surgeon_request_additional_instance_assumed is True

    grasped = TwinEvent()
    grasped.event_type = "RobotGraspedTool"
    grasped.instrument_id = "T02"
    twin.apply_event(grasped)
    assert second_state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT

    event = TwinEvent()
    event.event_type = "ShadowAdditionalToolHandoverCompleted"
    event.instrument_id = "T02"
    event.detail_json = '{"ground_truth_used":false}'
    twin.apply_event(event)

    assert twin.state.surgeon_request_tool == ""
    assert twin.state.surgeon_request_additional_instance_assumed is False
    assert first_state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert second_state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED

    assert twin.update_explicit_request("Adson 하나 더") == "T02"
    assert twin.state.surgeon_request_tool == ""
    assert twin.state.surgeon_request_generation == 0
    assert first_generation > 0


def test_shadow_public_request_does_not_invent_mayo_placement():
    twin = ORDigitalTwin(
        _thyroid_spec(),
        allow_shadow_request_capacity_reconciliation=True,
        allow_shadow_type_instance_requests=True,
    )
    for index, tool_id in enumerate(("T02", "T03"), start=1):
        twin._set_lifecycle(
            twin.instrument_states[f"{tool_id}#1"],
            LIFECYCLE_SURGEON_OWNED,
            location_type="surgeon_hand",
            location_id="surgeon_hand",
            confidence=1.0,
            last_update_sec=float(index),
        )

    assert twin.update_explicit_request("Bovie") == "T04"
    assumptions = twin.drain_shadow_assumption_audit()

    assert len(assumptions) == 1
    assert assumptions[0]["event_type"] == "ShadowPublicRequestHandCapacityReconciled"
    assert assumptions[0]["ground_truth_used"] is False
    assert twin.instrument_states["T02#1"].lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert twin.instrument_states["T02#1"].location_type == "surgical_field"
    assert twin.instrument_states["T03#1"].lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert twin.instrument_states["T03#1"].location_type == "surgeon_hand"
    assert twin.handover_allowed() is True


def test_new_voice_request_supersedes_uncommitted_blocked_request():
    twin = ORDigitalTwin(_thyroid_spec())
    for index, tool_id in enumerate(("T02", "T03"), start=1):
        twin._set_lifecycle(
            twin.instrument_states[f"{tool_id}#1"],
            LIFECYCLE_SURGEON_OWNED,
            location_type="surgeon_hand",
            location_id="surgeon_hand",
            confidence=1.0,
            last_update_sec=float(index),
        )

    assert twin.update_explicit_request("Bovie") == "T04"
    assert twin.handover_allowed() is False
    assert twin.update_explicit_request("bipolar") == "T07"

    assert twin.request_queue_summary()["queued_tools"] == ["T07"]
    assert twin.state.surgeon_request_tool == "T07"
    assert any(
        event["event_type"] == "SurgeonRequestSuperseded"
        and event["superseded_tool"] == "T04"
        and event["incoming_tool"] == "T07"
        for event in twin.event_history
    )


def test_plain_repeat_of_field_deployed_tool_selects_next_instance():
    twin = ORDigitalTwin(_thyroid_demo_spec())
    twin.state.filtered_phase = "P03"
    twin._set_lifecycle(
        twin.instrument_states["T05#1"],
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="field_region",
        confidence=1.0,
    )

    assert twin.update_explicit_request("army") == "T05"
    assert twin.state.surgeon_request_instance_id == "T05#2"


def test_one_more_request_queues_a_second_generation_without_coalescing():
    twin = ORDigitalTwin(_thyroid_demo_spec())

    assert twin.update_explicit_request("army") == "T05"
    assert twin.update_explicit_request("army 하나 더") == "T05"

    queued = list(twin.state.surgeon_request_queue)
    assert [cue.instrument_id for cue in queued] == ["T05", "T05"]
    assert [cue.instance_id for cue in queued] == ["T05#1", "T05#2"]
    assert queued[0].generation < queued[1].generation
    assert queued[0].shadow_additional_instance_assumed is False
    assert queued[1].shadow_additional_instance_assumed is True
    assert twin.drain_shadow_assumption_audit() == []


def test_repeated_tool_name_without_additional_cue_is_coalesced():
    twin = ORDigitalTwin(
        _thyroid_spec(),
        allow_shadow_request_capacity_reconciliation=True,
        allow_shadow_type_instance_requests=True,
    )

    assert twin.update_explicit_request("army army") == "T05"

    assert len(twin.state.surgeon_request_queue) == 1
    assert twin.drain_shadow_assumption_audit() == []


def test_voice_request_bypasses_only_vlm_and_phase_inference_guards():
    twin = ORDigitalTwin(_thyroid_spec())
    twin.state.phase_uncertain = True
    twin.set_safety_flag("vlm_unhealthy", True)
    twin.update_explicit_request("#15 Scalpel please")

    assert twin.state.explicit_request_tool == "T01"
    assert twin.handover_allowed() is True

    twin.set_safety_flag("duplicate_tool_holder", True)

    assert twin.handover_allowed() is False


def test_transcript_callback_creates_request_without_structured_actor_message():
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = ORDigitalTwin(_thyroid_spec())
    node._tool_predict_stability = {}
    published_events = []
    published_world_states = []
    node._publish_event = lambda event_type, **kwargs: published_events.append(
        (event_type, kwargs)
    )
    node._publish_world_state = lambda: published_world_states.append(True)

    transcript = String()
    transcript.data = "Bovie surgical cautery please"
    node._on_request(transcript)

    assert node._twin.state.explicit_request_tool == "T04"
    assert published_events[0][0] == "VoiceTranscriptObserved"
    assert published_events[0][1]["instrument_id"] == "T04"
    assert published_world_states == [True]


def test_transcript_callback_starts_cleanup_for_explicit_completion_signal():
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = ORDigitalTwin(_thyroid_spec())
    node._twin.state.running = True
    node._twin.state.execution_state = "running"
    node._twin._set_lifecycle(
        node._twin.instrument_states["T04#1"],
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )
    node._tool_predict_stability = {}
    published_events = []
    published_world_states = []
    node._stamp = lambda: SurgeonRequest().stamp
    node._publish_event = lambda event_type, **kwargs: published_events.append(
        (event_type, kwargs)
    )
    node._publish_world_state = lambda: published_world_states.append(True)

    transcript = String()
    transcript.data = "네 마치겠습니다."
    node._on_request(transcript)

    assert node._twin.state.execution_state == "finishing"
    assert published_events[0][1]["detail"]["command_type"] == "procedure_completion"
    assert published_world_states == [True]


def test_partial_task_completion_is_not_procedure_completion():
    twin = ORDigitalTwin(_thyroid_spec())

    assert twin.is_explicit_procedure_completion_request("네 마치겠습니다.") is True
    assert twin.is_explicit_procedure_completion_request("the procedure is complete") is True
    assert twin.is_explicit_procedure_completion_request("지혈을 마치겠습니다") is False
    assert twin.is_explicit_procedure_completion_request("Bovie finished") is False


def test_group_voice_cue_is_not_misclassified_as_tool_handover():
    twin = ORDigitalTwin(_thyroid_spec())

    assert twin.is_explicit_voice_tool_request("석션 시작") is False
    assert twin.is_explicit_voice_tool_request("Bovie surgical cautery please") is True
    assert twin.is_explicit_voice_tool_request("자 이제 Adson 받고") is True
    assert twin.is_explicit_voice_tool_request("자 army 자 army 하나 더") is True
    assert twin.is_explicit_voice_tool_request(
        "현재 Bovie를 사용하고 있으며 다음 단계 준비를 계속합니다"
    ) is False


@pytest.mark.parametrize(
    "text",
    [
        "갑상선 절제술 시작하자",
        "일곱 번째 갑상선 절제술 시작",
        "네 자 열 번째 갑상선 절제술 시작하겠습니다.",
        "start thyroid surgery",
        "let us begin the thyroid procedure",
        "수술을 계속 진행하겠습니다",
    ],
)
def test_procedure_control_speech_is_not_a_tool_request(text: str):
    twin = ORDigitalTwin(_thyroid_demo_spec())

    assert twin.resolve_explicit_voice_tool_request(text) == ""
    assert twin.is_explicit_voice_tool_request(text) is False
    assert twin.update_explicit_request(text) == ""
    assert twin.state.surgeon_request_tool == ""


@pytest.mark.parametrize(
    "text",
    [
        "thyroid",
        "12th thyroid",
        "갑상선",
        "열두 번째 갑상선",
    ],
)
def test_bare_procedure_name_fragment_is_not_a_tool_request(text: str):
    twin = ORDigitalTwin(_thyroid_demo_spec())

    assert twin.resolve_explicit_voice_tool_request(text) == ""
    assert twin.update_explicit_request(text) == ""
    assert twin.state.surgeon_request_tool == ""


@pytest.mark.parametrize(
    ("text", "expected_tool"),
    [
        ("thyroid retractor", "T11"),
        ("thyroid please", "T11"),
        ("갑상선 리트랙터", "T11"),
        ("갑상선 주세요", "T11"),
        ("Adson", "T02"),
    ],
)
def test_procedure_name_tool_alias_requires_tool_class_or_request_marker(
    text: str,
    expected_tool: str,
):
    twin = ORDigitalTwin(_thyroid_demo_spec())

    assert twin.resolve_explicit_voice_tool_request(text) == expected_tool


def test_mixed_script_compact_additional_request_selects_second_instance():
    twin = ORDigitalTwin(_thyroid_demo_spec())

    assert twin.update_explicit_request("Adson") == "T02"
    assert twin.update_explicit_request("Adson하나 더") == "T02"
    assert [
        cue.instance_id
        for cue in twin.state.surgeon_request_queue
    ] == ["T02#1", "T02#2"]


@pytest.mark.parametrize(
    ("text", "expected_tool"),
    [
        ("자 여섯 번째 갑상선 절제술 시작 Adson", "T02"),
        ("수술 시작, Bovie 주세요", "T04"),
        ("thyroid procedure start; Adson", "T02"),
        ("갑상선 리트랙터 주세요", "T11"),
        ("thyroid retractor", "T11"),
    ],
)
def test_tool_request_after_procedure_control_clause_is_preserved(
    text: str,
    expected_tool: str,
):
    twin = ORDigitalTwin(_thyroid_demo_spec())

    assert twin.resolve_explicit_voice_tool_request(text) == expected_tool
    assert twin.is_explicit_voice_tool_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "suction이랑 Bovie tip 좀 닦아줘 볼래요",
        "Bovie를 세척해 주세요",
        "please clean the Bovie tip",
        "Bovie 정리해 주세요",
        "Bovie 빼 주세요",
    ],
)
def test_tool_management_speech_is_not_a_handover_request(text: str):
    twin = ORDigitalTwin(_thyroid_demo_spec())

    assert twin.resolve_explicit_voice_tool_request(text) == ""
    assert twin.is_explicit_voice_tool_request(text) is False


@pytest.mark.parametrize(
    ("text", "expected_tool"),
    [
        ("Adson 받고 suction 빼", "T02"),
        ("Bovie 빼고 bipolar 주세요", "T07"),
        ("please clean Bovie, then pass Adson", "T02"),
    ],
)
def test_compound_voice_routes_handover_clause_independently(
    text: str,
    expected_tool: str,
):
    twin = ORDigitalTwin(_thyroid_demo_spec())

    assert twin.resolve_explicit_voice_tool_request(text) == expected_tool
    assert twin.is_explicit_voice_tool_request(text) is True


@pytest.mark.parametrize(
    ("text", "expected_tool"),
    [
        ("Bovie 다시 줘", "T04"),
        ("Bovie 주세요", "T04"),
        ("please pass the Bovie", "T04"),
        ("I need the Bovie", "T04"),
    ],
)
def test_handover_verbs_remain_supported(text: str, expected_tool: str):
    twin = ORDigitalTwin(_thyroid_demo_spec())

    assert twin.resolve_explicit_voice_tool_request(text) == expected_tool
