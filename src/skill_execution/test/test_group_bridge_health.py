from __future__ import annotations

import threading
from types import SimpleNamespace

from skill_execution.group_bridge import (
    ActiveGroupGoal,
    BedRobotArmGroupActionBridge,
    GroupCommandEnvelope,
)


class _Client:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def server_is_ready(self) -> bool:
        return self.ready


def test_unchanged_readiness_is_suppressed_but_force_reannounces_it():
    bridge = BedRobotArmGroupActionBridge.__new__(BedRobotArmGroupActionBridge)
    bridge._lock = threading.RLock()
    bridge._action_clients = {
        "suction": _Client(False),
        "retraction": _Client(False),
    }
    bridge._action_names = {
        "suction": "/bed_robot_arm_group/suction/execute",
        "retraction": "/bed_robot_arm_group/retraction/execute",
    }
    bridge._server_ready = {"suction": None, "retraction": None}
    bridge._active = {}
    bridge._group_states = {"suction": "suctioning", "retraction": "holding"}
    bridge._command_started_ns = {}
    bridge._availability_envelope = lambda group_id: group_id
    published = []
    bridge._publish_status = lambda command, **kwargs: published.append(
        (
            command,
            kwargs["state"],
            kwargs["error_code"],
            kwargs["update_group_state"],
        )
    )
    bridge.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(nanoseconds=1_000_000_000)
    )

    bridge._watch_action_health()
    bridge._watch_action_health()

    assert published == [
        ("suction", "offline", "server_unavailable", False),
        ("retraction", "offline", "server_unavailable", False),
    ]
    assert bridge._group_states == {
        "suction": "suctioning",
        "retraction": "holding",
    }

    for client in bridge._action_clients.values():
        client.ready = True
    bridge._watch_action_health()
    assert published[-2:] == [
        ("suction", "suctioning", "", False),
        ("retraction", "holding", "", False),
    ]

    bridge._watch_action_health()
    assert len(published) == 4

    bridge._watch_action_health(force_availability=True)
    assert published[-2:] == [
        ("suction", "suctioning", "", False),
        ("retraction", "holding", "", False),
    ]


def test_reconnect_after_dispatch_offline_recovers_operational_state_to_standby():
    bridge = BedRobotArmGroupActionBridge.__new__(BedRobotArmGroupActionBridge)
    bridge._lock = threading.RLock()
    bridge._action_clients = {"suction": _Client(True)}
    bridge._action_names = {"suction": "/bed_robot_arm_group/suction/execute"}
    bridge._server_ready = {"suction": False}
    bridge._active = {}
    bridge._group_states = {"suction": "offline"}
    bridge._command_started_ns = {}
    bridge._availability_envelope = lambda group_id: group_id
    published = []
    bridge._publish_status = lambda command, **kwargs: published.append(
        (command, kwargs["state"], kwargs["success"])
    )
    bridge.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(nanoseconds=1_000_000_000)
    )

    bridge._watch_action_health()

    assert bridge._group_states["suction"] == "standby"
    assert published == [("suction", "standby", True)]


def test_feedback_is_coalesced_to_state_changes_and_quarter_progress_milestones():
    bridge = BedRobotArmGroupActionBridge.__new__(BedRobotArmGroupActionBridge)
    bridge._lock = threading.RLock()
    command = GroupCommandEnvelope(
        request_id="request-1",
        command_id="command-1",
        group_id="suction",
        operation="suction_start",
        direction="",
        distance_mm=0.0,
        distance_origin="",
        raw_distance_text="",
        end_effector_profile="suction",
        rationale="test",
        confidence=1.0,
    )
    bridge._active = {
        "suction": ActiveGroupGoal(command=command, signature=("request-1",))
    }
    bridge._cancelled_command_ids = set()
    bridge._timed_out_command_ids = set()
    clock_ns = [1_000_000_000]
    bridge.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(nanoseconds=clock_ns[0])
    )
    published = []
    bridge._publish_status = lambda _command, **kwargs: published.append(
        (kwargs["state"], kwargs["progress"])
    )

    def feedback(state: str, progress: float):
        return SimpleNamespace(
            feedback=SimpleNamespace(
                state=state,
                progress=progress,
                message=f"{state}:{progress}",
            )
        )

    for progress in (0.05, 0.10, 0.24, 0.25, 0.30, 0.51, 0.74, 0.76, 0.95):
        clock_ns[0] += 10_000_000
        bridge._on_feedback(command, feedback("suctioning", progress))
    bridge._on_feedback(command, feedback("stopping", 0.95))

    assert published == [
        ("suctioning", 0.05),
        ("suctioning", 0.25),
        ("suctioning", 0.51),
        ("suctioning", 0.76),
        ("stopping", 0.95),
    ]
    assert bridge._active["suction"].last_activity_ns == clock_ns[0]
