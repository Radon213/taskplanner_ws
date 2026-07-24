from __future__ import annotations

import threading
from types import SimpleNamespace

from skill_execution.group_bridge import BedRobotArmGroupActionBridge


class _Client:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def server_is_ready(self) -> bool:
        return self.ready


def test_offline_readiness_is_republished_as_a_periodic_heartbeat():
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
    bridge._server_ready = {"suction": False, "retraction": False}
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
