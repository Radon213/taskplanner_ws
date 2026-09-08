from __future__ import annotations

from integration_debug.status_composition import compose_observer_status


def _observer_status() -> dict[str, object]:
    return {
        "schema": "taskplanner.integration_debug.status.v1",
        "capabilities": [
            {"name": "observer", "enabled": True},
            {"name": "control", "enabled": False},
            {"name": "asr", "enabled": False},
        ],
        "runtime": {"network": {"interface_kind": "observer"}},
        "inputs": [{"topic": "/camera"}],
        "session": {"armed": False},
        "action": {"terminal": True},
        "endpoints": [],
        "outputs": [],
        "voice": {},
        "asr": {"state": "UNAVAILABLE"},
        "surgery_record": {"state": "IDLE"},
        "recent_events": [],
    }


def _control_status() -> dict[str, object]:
    return {
        "schema": "taskplanner.integration_debug.status.v1",
        "capabilities": [
            {"name": "observer", "enabled": True},
            {"name": "control", "enabled": True},
            {"name": "asr", "enabled": False},
        ],
        "runtime": {"manual_control_available": True, "network": {"interface_kind": "control"}},
        "session": {"armed": True},
        "action": {"terminal": False, "state": "executing"},
        "endpoints": [{"name": "tool_handover", "ready": True}],
        "outputs": [{"topic": "/debug", "enabled": True}],
        "voice": {"auto_execute": True},
        "asr": {"state": "UNAVAILABLE"},
        "surgery_record": {"state": "IDLE"},
        "recent_events": [{"event_type": "command_started"}],
    }


def test_control_state_is_composed_without_replacing_observer_inputs() -> None:
    composed = compose_observer_status(_observer_status(), _control_status())

    assert composed["inputs"] == [{"topic": "/camera"}]
    assert composed["session"] == {"armed": True}
    assert composed["action"] == {"terminal": False, "state": "executing"}
    assert composed["runtime"]["manual_control_available"] is True
    assert composed["runtime"]["network"] == {"interface_kind": "observer"}
    assert {row["name"]: row["enabled"] for row in composed["capabilities"]} == {
        "asr": False,
        "control": True,
        "observer": True,
    }


def test_invalid_private_status_does_not_change_observer_projection() -> None:
    observer = _observer_status()
    assert compose_observer_status(observer, {"schema": "wrong"}) == observer
