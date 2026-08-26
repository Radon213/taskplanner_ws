import threading

from integration_debug.node import IntegrationDebugNode
from integration_debug.retractor_health import VLMRuntimeStatus


def _operational_status(
    *,
    allowed: bool,
    control_window_open: bool | None = None,
    execution_state: str = "paused",
    reason: str = "",
    active_robot_task_id: str | None = None,
    robot_state: str | None = None,
    cleaner_busy: bool = False,
) -> dict[str, object]:
    window_open = allowed if control_window_open is None else control_window_open
    return {
        "received": True,
        "running": execution_state == "paused",
        "execution_state": execution_state,
        "active_robot_task_id": (
            "" if allowed else "task-17"
        ) if active_robot_task_id is None else active_robot_task_id,
        "robot_state": (
            "idle" if allowed else "moving"
        ) if robot_state is None else robot_state,
        "cleaner_busy": cleaner_busy,
        "intervention_allowed": allowed,
        "intervention_block_reason": reason,
        "control_window_open": window_open,
        "control_window_block_reason": "" if window_open else reason,
    }


def test_integrated_manual_write_uses_state_gate_without_planner_ack() -> None:
    class Harness:
        pass

    harness = Harness()
    harness._network_locked_to_runtime = True
    harness._lock = threading.RLock()
    harness._armed = True
    harness._fault_locked = False
    harness._manual_control_scope = "all"
    harness._acknowledged_blocked_nodes = set()
    harness._operational_runtime_status = lambda: _operational_status(allowed=True)

    assert IntegrationDebugNode._manual_write_block_reason(harness) == ""

    harness._operational_runtime_status = lambda: _operational_status(
        allowed=False,
        control_window_open=False,
        execution_state="running",
        reason="pause or stop the operational scenario before manual control",
    )
    assert IntegrationDebugNode._manual_write_block_reason(harness) == (
        "pause or stop the operational scenario before manual control"
    )


def test_blocker_discovery_uses_state_gate_only_for_integrated_debug() -> None:
    class Harness:
        pass

    harness = Harness()
    harness._network_locked_to_runtime = True
    harness._detected_planner_nodes = lambda: ["simulation_manager", "tree_executor"]
    harness._operational_runtime_status = lambda: _operational_status(
        allowed=False,
        control_window_open=False,
        execution_state="running",
        reason="pause or stop the operational scenario before manual control",
    )

    assert IntegrationDebugNode._blocked_nodes(harness) == [
        "operational_runtime_intervention_gate"
    ]

    harness._network_locked_to_runtime = False
    assert IntegrationDebugNode._blocked_nodes(harness) == [
        "simulation_manager",
        "tree_executor",
    ]


def test_standalone_planner_detection_cannot_be_bypassed_by_scope_or_ack() -> None:
    class Harness:
        pass

    harness = Harness()
    harness._network_locked_to_runtime = False
    harness._lock = threading.RLock()
    harness._fault_locked = False
    harness._armed = False
    harness._planner_coexistence_allowed = True
    harness._blocked_nodes = lambda: ["simulation_manager", "tree_executor"]

    accepted, command_id, message = IntegrationDebugNode._execute_command(
        harness,
        "arm",
        {
            "manual_control_scope": "tool_handover",
            "planner_coexistence_confirmed": True,
            "acknowledged_blocked_nodes": ["simulation_manager", "tree_executor"],
        },
    )

    assert accepted is False
    assert command_id == ""
    assert message == (
        "full Taskplanner nodes are active: simulation_manager, tree_executor"
    )
    assert harness._armed is False


def test_standalone_armed_session_cannot_write_after_planner_appears() -> None:
    class Harness:
        pass

    harness = Harness()
    harness._network_locked_to_runtime = False
    harness._lock = threading.RLock()
    harness._fault_locked = False
    harness._armed = True
    harness._manual_control_scope = "tool_handover"
    harness._planner_coexistence_allowed = True
    harness._acknowledged_blocked_nodes = {"simulation_manager", "tree_executor"}
    harness._blocked_nodes = lambda: ["simulation_manager", "tree_executor"]

    assert IntegrationDebugNode._manual_write_block_reason(
        harness,
        "tool_handover",
    ) == "full Taskplanner nodes are active: simulation_manager, tree_executor"


def test_integrated_arm_accepts_paused_state_without_coexistence_payload() -> None:
    class Harness:
        pass

    harness = Harness()
    harness._network_locked_to_runtime = True
    harness._lock = threading.RLock()
    harness._fault_locked = False
    harness._armed = False
    harness._manual_control_scope = "none"
    harness._acknowledged_blocked_nodes = {"tree_executor"}
    harness._last_heartbeat_monotonic = 0.0
    harness._last_error = "old"
    harness._operational_runtime_status = lambda: _operational_status(allowed=True)

    accepted, command_id, message = IntegrationDebugNode._execute_command(
        harness,
        "arm",
        {},
    )

    assert accepted is True
    assert command_id == ""
    assert message == "manual control armed while operational scenario is paused"
    assert harness._armed is True
    assert harness._acknowledged_blocked_nodes == set()


def test_integrated_arm_rejects_running_state_before_acquiring_authority() -> None:
    class Harness:
        pass

    harness = Harness()
    harness._network_locked_to_runtime = True
    harness._lock = threading.RLock()
    harness._fault_locked = False
    harness._armed = False
    harness._operational_runtime_status = lambda: _operational_status(
        allowed=False,
        control_window_open=False,
        execution_state="running",
        reason="pause or stop the operational scenario before manual control",
    )

    accepted, command_id, message = IntegrationDebugNode._execute_command(
        harness,
        "arm",
        {
            "planner_coexistence_confirmed": True,
            "acknowledged_blocked_nodes": ["tree_executor"],
        },
    )

    assert accepted is False
    assert command_id == ""
    assert message == "pause or stop the operational scenario before manual control"
    assert harness._armed is False


def _runtime_safety_harness(status: dict[str, object], *, command_id: str = ""):
    class Asr:
        def stop_async(self) -> None:
            return None

    class Harness:
        pass

    harness = Harness()
    harness._lock = threading.RLock()
    harness._auxiliary_lock = threading.RLock()
    harness._network_locked_to_runtime = True
    harness._armed = True
    harness._last_heartbeat_monotonic = 0.0
    harness._heartbeat_timeout_sec = 5.0
    harness._acknowledged_blocked_nodes = set()
    harness._active_command_id = command_id
    harness._operational_runtime_status = lambda: status
    harness._asr = Asr()
    harness._release_manual_publishers = lambda: None
    harness._disarm_locked = lambda: setattr(harness, "_armed", False)
    harness.events = []
    harness._record = lambda event_type, payload: harness.events.append(
        (event_type, payload)
    )
    harness.cancelled = []
    harness._request_cancel = lambda: harness.cancelled.append(command_id)
    return harness


def test_integrated_gate_disarms_when_paused_scenario_resumes() -> None:
    status = _operational_status(
        allowed=False,
        control_window_open=False,
        execution_state="running",
        reason="pause or stop the operational scenario before manual control",
    )
    harness = _runtime_safety_harness(status)

    IntegrationDebugNode._check_runtime_safety(harness)

    assert harness._armed is False
    assert harness.events[-1][0] == "operational_intervention_gate_closed"
    assert harness.cancelled == []


def test_admitted_command_is_not_cancelled_by_its_own_robot_activity() -> None:
    status = _operational_status(
        allowed=False,
        control_window_open=True,
        reason="wait for the active robot task to finish before manual control",
        active_robot_task_id="",
    )
    harness = _runtime_safety_harness(status, command_id="debug-command-1")

    IntegrationDebugNode._check_runtime_safety(harness)

    assert harness._armed is True
    assert harness.events == []
    assert harness.cancelled == []


def test_admitted_command_accepts_matching_operational_task_identity() -> None:
    status = _operational_status(
        allowed=False,
        control_window_open=True,
        active_robot_task_id="debug-A",
        robot_state="moving",
    )
    harness = _runtime_safety_harness(status, command_id="debug-A")

    IntegrationDebugNode._check_runtime_safety(harness)

    assert harness._armed is True
    assert harness.events == []
    assert harness.cancelled == []


def test_other_operational_task_revokes_and_cancels_debug_command() -> None:
    status = _operational_status(
        allowed=False,
        control_window_open=True,
        active_robot_task_id="debug-B",
        robot_state="moving",
    )
    harness = _runtime_safety_harness(status, command_id="debug-A")

    IntegrationDebugNode._check_runtime_safety(harness)

    assert harness._armed is False
    assert harness.events[-1][0] == "operational_intervention_gate_closed"
    assert "not owned" in harness.events[-1][1]["reason"]
    assert harness.cancelled == ["debug-A"]


def test_cleaner_activity_revokes_even_a_matching_debug_command() -> None:
    status = _operational_status(
        allowed=False,
        control_window_open=True,
        active_robot_task_id="debug-A",
        robot_state="cleaning",
        cleaner_busy=True,
    )
    harness = _runtime_safety_harness(status, command_id="debug-A")

    IntegrationDebugNode._check_runtime_safety(harness)

    assert harness._armed is False
    assert "cleaner activity" in harness.events[-1][1]["reason"]
    assert harness.cancelled == ["debug-A"]


def test_robot_fault_revokes_even_a_matching_debug_command() -> None:
    status = _operational_status(
        allowed=False,
        control_window_open=True,
        active_robot_task_id="debug-A",
        robot_state="fault",
    )
    harness = _runtime_safety_harness(status, command_id="debug-A")

    IntegrationDebugNode._check_runtime_safety(harness)

    assert harness._armed is False
    assert "cannot be owned" in harness.events[-1][1]["reason"]
    assert harness.cancelled == ["debug-A"]


def test_standalone_runtime_monitor_disarms_even_a_legacy_acked_session() -> None:
    harness = _runtime_safety_harness(_operational_status(allowed=True))
    harness._network_locked_to_runtime = False
    harness._acknowledged_blocked_nodes = {"simulation_manager", "tree_executor"}
    harness._blocked_nodes = lambda: ["simulation_manager", "tree_executor"]

    IntegrationDebugNode._check_runtime_safety(harness)

    assert harness._armed is False
    assert harness.events[-1][0] == "planner_coexistence_changed"


def test_vlm_observation_refresh_remains_available_without_manual_authority() -> None:
    class Harness:
        pass

    harness = Harness()
    harness._submit_vlm_observation = lambda **_kwargs: True
    harness._vlm_status_snapshot = lambda: {"loaded": True}

    accepted, command_id, message, result = (
        IntegrationDebugNode._handle_vlm_command(harness, "vlm_refresh", {})
    )

    assert accepted is True
    assert command_id == ""
    assert message == "VLM refresh submitted"
    assert result == {"loaded": True}


def test_integrated_vlm_load_requires_paused_or_stopped_armed_authority() -> None:
    class Runtime:
        calls = 0

        @classmethod
        def load(cls):
            cls.calls += 1
            raise AssertionError("blocked VLM load must not reach the shared runtime")

    class Harness:
        pass

    harness = Harness()
    harness._network_locked_to_runtime = True
    harness._lock = threading.RLock()
    harness._fault_locked = False
    harness._armed = True
    harness._manual_control_scope = "all"
    harness._vlm_runtime = Runtime()
    harness._vlm_status_snapshot = lambda: {"load_state": "loaded"}
    harness._operational_runtime_status = lambda: _operational_status(
        allowed=False,
        control_window_open=False,
        execution_state="running",
        reason="pause or stop the operational scenario before manual control",
    )
    harness._manual_write_block_reason = (
        IntegrationDebugNode._manual_write_block_reason.__get__(harness)
    )

    accepted, command_id, message, result = (
        IntegrationDebugNode._handle_vlm_command(harness, "vlm_load", {})
    )

    assert accepted is False
    assert command_id == ""
    assert message == "pause or stop the operational scenario before manual control"
    assert result == {"load_state": "loaded"}
    assert Runtime.calls == 0

    harness._operational_runtime_status = lambda: _operational_status(allowed=True)
    harness._armed = False
    accepted, _command_id, message, _result = (
        IntegrationDebugNode._handle_vlm_command(harness, "vlm_load", {})
    )

    assert accepted is False
    assert message == "manual control is not armed"
    assert Runtime.calls == 0


def test_integrated_vlm_load_reaches_shared_runtime_after_gate() -> None:
    class Runtime:
        calls = 0

        @classmethod
        def load(cls):
            cls.calls += 1
            return VLMRuntimeStatus(
                manager_reachable=True,
                catalog_reachable=True,
                load_state="loading",
                loaded=False,
                available=True,
                runtime_managed=True,
                detail="loading fixed model",
            )

    class Harness:
        pass

    harness = Harness()
    harness._network_locked_to_runtime = True
    harness._manual_write_block_reason = lambda operation="": (
        "" if operation == "vlm_load" else "unexpected operation"
    )
    harness._vlm_runtime = Runtime()
    harness._lock = threading.RLock()
    harness._pending_vlm_observation = None
    harness._vlm_explicit_probe_requested = False
    harness._last_vlm_observation_submitted_monotonic = 3.0
    harness._apply_vlm_observation = lambda runtime, _health: None
    harness._vlm_status_snapshot = lambda: {"load_state": "loading"}

    accepted, command_id, message, result = (
        IntegrationDebugNode._handle_vlm_command(harness, "vlm_load", {})
    )

    assert accepted is True
    assert command_id == ""
    assert message == "loading fixed model"
    assert result == {"load_state": "loading"}
    assert Runtime.calls == 1
    assert harness._last_vlm_observation_submitted_monotonic == 0.0


def test_operational_asr_recording_controls_remain_observation_only() -> None:
    class Harness:
        pass

    harness = Harness()
    calls: list[str] = []
    harness._proxy_operational_asr_control = lambda operation: (
        calls.append(operation) or (True, "", "ok", {"recording": True})
    )

    started = IntegrationDebugNode._handle_asr_command(
        harness,
        "asr_recording_start",
        {},
    )
    stopped = IntegrationDebugNode._handle_asr_command(
        harness,
        "asr_recording_stop",
        {},
    )

    assert started[0] is True
    assert stopped[0] is True
    assert calls == ["start_recording", "stop_recording"]
