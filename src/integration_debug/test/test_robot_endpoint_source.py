from __future__ import annotations

import threading

from procedure_spec import RetractionState

from integration_debug.node import IntegrationDebugNode


class _ActionClient:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def server_is_ready(self) -> bool:
        return self.ready


class _ServiceClient:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def service_is_ready(self) -> bool:
        return self.ready


def _harness(*, armed: bool = False, virtual_enabled: bool = True):
    class Harness:
        pass

    harness = Harness()
    harness._lock = threading.RLock()
    harness._active_command_id = ""
    harness._armed = armed
    harness._virtual_robot_enabled = virtual_enabled
    harness._robot_endpoint_source = "external"
    harness._external_tool_client = _ActionClient(True)
    harness._virtual_tool_client = _ActionClient(True)
    harness._external_retraction_client = _ServiceClient(False)
    harness._virtual_retraction_client = _ServiceClient(True)
    harness._tool_client = harness._external_tool_client
    harness._retraction_client = harness._external_retraction_client
    harness._external_retraction_service_name = "/surgery/retraction/command"
    harness._virtual_retraction_service_name = (
        "/integration/debug/virtual/retraction/command"
    )
    harness._virtual_tool_handover_name = (
        "/integration/debug/virtual/tool_handover"
    )
    harness._virtual_bed_robot_status_topic = (
        "/integration/debug/virtual/bed_robot_arms/status"
    )
    harness._retraction_service_name = harness._external_retraction_service_name
    harness._bed_robot_arm_status_sources = {"external": {}, "virtual": {}}
    harness._bed_robot_arm_status_max_age_sec = 3.0
    harness._bed_robot_arm_status_received = False
    harness._bed_robot_arm_status_received_monotonic = 0.0
    harness._bed_robot_arm_status_source_stamp_sec = 0.0
    harness._bed_robot_arm_status_revision = None
    harness._bed_robot_arm_status_summary = {}
    harness._retraction_state = RetractionState.RETRACTION_ACTIVE
    harness._retraction_voice_auto_dispatch = True
    harness._retraction_voice_generation = 4
    harness._last_retraction_rejection_reason = "old"
    harness.events = []
    harness._record = lambda event, payload: harness.events.append((event, payload))
    harness._bed_robot_arm_source_ready = (
        IntegrationDebugNode._bed_robot_arm_source_ready.__get__(harness)
    )
    harness._robot_source_snapshot = IntegrationDebugNode._robot_source_snapshot.__get__(
        harness
    )
    return harness


def test_explicit_source_switch_moves_all_three_selected_interfaces() -> None:
    harness = _harness()

    accepted, command_id, message, status = (
        IntegrationDebugNode._configure_robot_endpoint_source(
            harness,
            {"source": "virtual"},
        )
    )

    assert accepted is True
    assert command_id == ""
    assert "state reset to idle" in message
    assert harness._robot_endpoint_source == "virtual"
    assert harness._tool_client is harness._virtual_tool_client
    assert harness._retraction_client is harness._virtual_retraction_client
    assert harness._retraction_service_name.endswith("/virtual/retraction/command")
    assert harness._retraction_state is RetractionState.IDLE
    assert harness._retraction_voice_auto_dispatch is False
    assert harness._retraction_voice_generation == 5
    assert status["selected_source"] == "virtual"
    assert status["tool_handover_ready"] is True
    assert status["retraction_service_ready"] is True
    assert harness.events == [
        (
            "robot_endpoint_source_changed",
            {"previous_source": "external", "selected_source": "virtual"},
        )
    ]


def test_source_switch_never_auto_falls_back_and_requires_disarmed_session() -> None:
    armed = _harness(armed=True)
    result = IntegrationDebugNode._configure_robot_endpoint_source(
        armed, {"source": "virtual"}
    )
    assert result[0] is False
    assert armed._robot_endpoint_source == "external"

    disabled = _harness(virtual_enabled=False)
    result = IntegrationDebugNode._configure_robot_endpoint_source(
        disabled, {"source": "virtual"}
    )
    assert result[0] is False
    assert disabled._robot_endpoint_source == "external"


def test_readiness_reports_selected_source_without_mixing_clients() -> None:
    harness = _harness()

    external = IntegrationDebugNode._robot_source_snapshot(harness)
    assert external["selected_source"] == "external"
    assert external["tool_handover_ready"] is True
    assert external["retraction_service_ready"] is False
    assert external["virtual_retraction_service_ready"] is True

    IntegrationDebugNode._configure_robot_endpoint_source(
        harness, {"source": "virtual"}
    )
    virtual = IntegrationDebugNode._robot_source_snapshot(harness)
    assert virtual["selected_source"] == "virtual"
    assert virtual["retraction_service_ready"] is True
