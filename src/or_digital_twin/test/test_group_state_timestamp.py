from __future__ import annotations

from pathlib import Path

from or_digital_twin.node import ORDigitalTwinNode
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import BedRobotArmGroupCommand, BedRobotArmGroupStatus


def _thyroid_spec():
    return load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
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
