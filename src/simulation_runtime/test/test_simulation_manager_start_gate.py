"""Focused state-core tests after ScenarioStore ownership extraction."""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

try:
    from btops_interfaces import msg as _btops_msg  # noqa: F401
    from btops_interfaces import srv as _btops_srv  # noqa: F401
except ModuleNotFoundError:
    btops_package = types.ModuleType("btops_interfaces")
    btops_msg = types.ModuleType("btops_interfaces.msg")
    btops_srv = types.ModuleType("btops_interfaces.srv")
    btops_msg.ExecutionSnapshot = type("ExecutionSnapshot", (), {})
    btops_srv.CommandExecutor = type("CommandExecutor", (), {})
    btops_srv.GetRuntimeState = type("GetRuntimeState", (), {})
    btops_srv.StartBehavior = type(
        "StartBehavior",
        (),
        {"Request": type("StartBehaviorRequest", (), {})},
    )
    btops_package.msg = btops_msg
    btops_package.srv = btops_srv
    sys.modules["btops_interfaces"] = btops_package
    sys.modules["btops_interfaces.msg"] = btops_msg
    sys.modules["btops_interfaces.srv"] = btops_srv

import simulation_runtime.simulation_manager as manager_module
from simulation_runtime.scenario_store import ScenarioSnapshot, scenario_config_json
from simulation_runtime.simulation_manager import SimulationManagerNode


class _Logger:
    def info(self, _message: str, **_kwargs) -> None:
        pass

    def warning(self, _message: str, **_kwargs) -> None:
        pass

    def warn(self, _message: str, **_kwargs) -> None:
        pass

    def error(self, _message: str, **_kwargs) -> None:
        pass


class _Publisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, message) -> None:
        self.messages.append(message)


def _revision(char: str) -> str:
    return f"sha256:{char * 64}"


def _scenario_manager(*, running: bool = False, state: str = "idle"):
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._spec_root = Path("/tmp/specs")
    manager._active_bundle = "thyroidectomy"
    manager._active_spec_dir = Path("/tmp/specs/thyroidectomy")
    manager._active_spec = SimpleNamespace(default_phase_id="P01")
    manager._active_config_revision = _revision("0")
    manager._scenario_config_revision = _revision("0")
    manager._pending_scenario_config = None
    manager._scenario_config_lock = threading.RLock()
    manager._running = running
    manager._execution_state = state
    manager._latest_state_lock = threading.Lock()
    manager._latest_state = None
    manager.get_logger = lambda: _Logger()
    return manager


def _candidate(bundle_name: str = "thyroidectomy_demo") -> ScenarioSnapshot:
    return ScenarioSnapshot(
        bundle_name=bundle_name,
        spec_dir=Path(f"/tmp/specs/{bundle_name}"),
        revision=_revision("1"),
        spec=SimpleNamespace(default_phase_id="P01"),
    )


def test_state_core_has_no_direct_scenario_selection_or_preflight_owner() -> None:
    assert not hasattr(SimulationManagerNode, "_handle_select_bundle")
    assert not hasattr(SimulationManagerNode, "_set_spec_dir_on_runtime")
    assert not hasattr(SimulationManagerNode, "_configure_integration_preflight")
    assert not hasattr(SimulationManagerNode, "_check_integration_preflight")
    source = Path(manager_module.__file__).read_text(encoding="utf-8")
    assert "require_integration_preflight" not in source
    assert "integration-preflight-gated" not in source


def test_state_core_observes_latched_scenario_store_snapshot_without_republishing(
    monkeypatch,
) -> None:
    manager = _scenario_manager()
    candidate = _candidate()
    monkeypatch.setattr(
        manager_module,
        "load_scenario_snapshot",
        lambda *_args, **_kwargs: candidate,
    )

    manager._on_scenario_config(SimpleNamespace(data=scenario_config_json(candidate)))

    assert manager._active_bundle == candidate.bundle_name
    assert manager._active_spec_dir == candidate.spec_dir
    assert manager._active_config_revision == candidate.revision
    assert manager._pending_scenario_config is None
    assert not hasattr(manager, "_scenario_config_pub")


def test_scenario_snapshot_waits_for_authoritative_pause_then_applies(monkeypatch) -> None:
    manager = _scenario_manager(running=True, state="running")
    candidate = _candidate()
    manager._latest_state = SimpleNamespace(running=True, execution_state="running")
    monkeypatch.setattr(
        manager_module,
        "load_scenario_snapshot",
        lambda *_args, **_kwargs: candidate,
    )

    manager._on_scenario_config(SimpleNamespace(data=scenario_config_json(candidate)))

    assert manager._active_bundle == "thyroidectomy"
    assert manager._pending_scenario_config == candidate

    manager._latest_state = SimpleNamespace(running=True, execution_state="paused")
    manager._apply_pending_scenario_config_if_quiescent()

    assert manager._active_bundle == candidate.bundle_name
    assert manager._scenario_config_revision == candidate.revision
    assert manager._pending_scenario_config is None


def test_bad_scenario_store_revision_is_ignored(monkeypatch) -> None:
    manager = _scenario_manager()
    candidate = _candidate()
    published = ScenarioSnapshot(
        bundle_name=candidate.bundle_name,
        spec_dir=candidate.spec_dir,
        revision=_revision("2"),
        spec=candidate.spec,
    )
    monkeypatch.setattr(
        manager_module,
        "load_scenario_snapshot",
        lambda *_args, **_kwargs: candidate,
    )

    manager._on_scenario_config(SimpleNamespace(data=scenario_config_json(published)))

    assert manager._active_bundle == "thyroidectomy"
    assert manager._pending_scenario_config is None


def test_start_sequence_preserves_current_twin_layout_without_an_implicit_reset() -> None:
    events: list[str] = []
    manager = _scenario_manager()
    manager._operation_cancel = threading.Event()
    manager._completion_terminate_started = False
    manager._normalize_start_phase = lambda phase: phase

    def _prepare_executor() -> None:
        # The no-op executor fast path is keyed from the pre-start lifecycle
        # state.  Advancing this to "starting" before the check would force a
        # needless terminate request for an idle executor.
        assert manager._running is False
        assert manager._execution_state == "idle"
        events.append("executor-idle")

    manager._prepare_executor_for_restart = _prepare_executor
    manager._publish_control = lambda command, **kwargs: events.append(
        f"control:{command}:{kwargs}"
    )
    manager._executor_state_event = lambda: ("terminated", 12, time.monotonic())
    manager._start_behavior = lambda **_kwargs: events.append("start-behavior") or (
        True,
        "accepted",
    )
    manager._wait_for_executor_running = lambda **_kwargs: events.append(
        "executor-running"
    ) or True
    manager._commit_start_actors = lambda _phase="": events.append("start-actors")

    assert manager._start_sequence() == "accepted"
    assert events == [
        "executor-idle",
        "control:start_runtime:{'repeat_count': 1, 'wait_for_subscriber': False}",
        "start-behavior",
        "executor-running",
        "start-actors",
    ]
    source = Path(manager_module.__file__).read_text(encoding="utf-8")
    assert "initial idle digital twin frame" not in source
    assert "digital twin did not enter running state before BT start" not in source


def test_initial_executor_check_uses_the_short_start_path_probe() -> None:
    manager = _scenario_manager()
    manager._executor_settled_confirmed = True
    manager._executor_state_event = lambda: ("", 0, 0.0)
    captured: dict[str, float] = {}

    def _runtime_state(**kwargs):
        captured.update(kwargs)
        return True, "idle", "ready"

    manager._get_runtime_state_detail = _runtime_state
    manager._command_executor = lambda _command: (_ for _ in ()).throw(
        AssertionError("idle executor must not be terminated")
    )

    manager._prepare_executor_for_restart()

    assert captured == {
        "service_timeout_sec": manager_module.PRESTART_EXECUTOR_SERVICE_TIMEOUT_SEC,
        "response_timeout_sec": manager_module.PRESTART_EXECUTOR_RESPONSE_TIMEOUT_SEC,
    }
    assert manager_module.PRESTART_EXECUTOR_SERVICE_TIMEOUT_SEC < 0.25
    assert manager_module.PRESTART_EXECUTOR_RESPONSE_TIMEOUT_SEC < 0.75


def test_resume_sequence_never_calls_an_integration_preflight_gate() -> None:
    events: list[str] = []
    manager = _scenario_manager(running=True, state="paused")
    manager._publish_control = lambda command: events.append(f"control:{command}")
    manager._wait_for_simulation_state = lambda *_args, **_kwargs: events.append("twin")
    manager._command_executor = lambda command: events.append(f"executor:{command}") or (
        True,
        "resumed",
    )
    manager._wait_for_executor_running = lambda **_kwargs: events.append(
        "executor-running"
    ) or True

    assert manager._resume_sequence() == "resumed"
    assert events == [
        "control:resume",
        "twin",
        "executor:resume",
        "executor-running",
    ]


def test_state_core_does_not_export_a_mode_transition_reservation_protocol() -> None:
    source = Path(manager_module.__file__).read_text(encoding="utf-8")

    assert "/simulation/check_transition_ready" not in source
    assert "/simulation/reserve_transition" not in source
    assert "dt_receipt_max_age" not in source
    assert not hasattr(SimulationManagerNode, "_handle_reserve_transition")


def _terminal_manager():
    manager = _scenario_manager(running=True, state="running")
    manager._active_procedure_run_id = "run-1"
    manager._active_procedure_bundle = "thyroidectomy"
    manager._terminal_auto_reset_lock = threading.Lock()
    manager._terminal_receipt_lock = threading.Lock()
    manager._last_terminal_receipt_run_id = ""
    manager._terminal_receipt_pub = _Publisher()
    manager._lifecycle_event_pub = _Publisher()
    manager.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(
            nanoseconds=1_234_000_000,
            to_msg=lambda: SimpleNamespace(sec=1, nanosec=234_000_000),
        )
    )
    return manager


def _terminal_state(*, running: bool = False, state: str = "halted", run_id: str = "run-1"):
    return SimpleNamespace(
        procedure_id="thyroidectomy",
        procedure_run_id=run_id,
        active_bundle="thyroidectomy",
        running=running,
        execution_state=state,
    )


def test_terminal_receipt_requires_same_run_and_exact_terminal_state() -> None:
    manager = _terminal_manager()

    assert not manager._publish_terminal_receipt(
        terminal_kind="stop",
        execution_state="halted",
        state=_terminal_state(running=True),
    )
    assert not manager._publish_terminal_receipt(
        terminal_kind="stop",
        execution_state="halted",
        state=_terminal_state(run_id="old-run"),
    )
    assert manager._publish_terminal_receipt(
        terminal_kind="stop",
        execution_state="halted",
        state=_terminal_state(),
        message="settled",
    )
    assert not manager._publish_terminal_receipt(
        terminal_kind="completed",
        execution_state="completed",
        state=_terminal_state(state="completed"),
    )

    assert len(manager._terminal_receipt_pub.messages) == 1
    payload = json.loads(manager._terminal_receipt_pub.messages[0].data)
    assert payload["procedure_run_id"] == "run-1"
    assert payload["terminal_kind"] == "stop"
    assert payload["execution_state"] == "halted"


def test_stop_receipt_is_emitted_only_after_halted_ack_and_new_executor_settlement() -> None:
    manager = _terminal_manager()
    calls: list[object] = []
    manager._latest_state = _terminal_state()
    manager._stop_digital_twin_to_halted = lambda: calls.append("halted") or True
    manager._executor_state_event = lambda: ("running", 41, time.monotonic())
    manager._command_executor = lambda command: calls.append(command) or (True, "stopped")
    manager._wait_for_executor_idle = lambda **kwargs: calls.append(
        ("settled", kwargs)
    ) or True
    manager._publish_terminal_receipt = lambda **kwargs: calls.append(
        ("receipt", kwargs)
    ) or True
    manager._reset_digital_twin_to_idle = lambda **kwargs: calls.append(
        ("reset", kwargs)
    )

    assert manager._stop_sequence() == "stopped; simulation runtime reset to idle"
    assert calls[0:2] == ["halted", "terminate"]
    assert calls[2] == (
        "settled",
        {"timeout_sec": 8.0, "after_generation": 41},
    )
    assert calls[3][0] == "receipt"
    assert calls[4] == ("reset", {"expected_bundle": "thyroidectomy"})
    assert manager._execution_state == "idle"
    assert manager._active_procedure_run_id == ""

    manager = _terminal_manager()
    manager._stop_digital_twin_to_halted = lambda: False
    manager._publish_terminal_receipt = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("failed stop must not publish a terminal receipt")
    )
    try:
        manager._stop_sequence()
    except RuntimeError as exc:
        assert "did not confirm halted" in str(exc)
    else:
        raise AssertionError("missing halted acknowledgement must fail Stop")


def test_completed_receipt_is_published_before_the_automatic_idle_reset() -> None:
    manager = _terminal_manager()
    terminal = _terminal_state(state="completed")
    manager._latest_state = terminal
    manager._completion_terminate_started = True
    calls: list[object] = []
    manager._executor_state_event = lambda: ("running", 73, time.monotonic())
    manager._command_executor = lambda command: calls.append(command) or (True, "terminated")
    manager._wait_for_executor_idle = lambda **kwargs: calls.append(
        ("settled", kwargs)
    ) or True

    def _reset(**kwargs) -> None:
        assert len(manager._terminal_receipt_pub.messages) == 1
        calls.append(("reset", kwargs))
        manager._set_idle_state()

    manager._reset_digital_twin_to_idle = _reset

    manager._terminate_executor_after_completion(terminal)

    assert calls[0] == "terminate"
    assert calls[1] == (
        "settled",
        {"timeout_sec": 8.0, "after_generation": 73},
    )
    assert calls[2] == ("reset", {"expected_bundle": "thyroidectomy"})
    payload = json.loads(manager._terminal_receipt_pub.messages[0].data)
    assert payload["terminal_kind"] == "completed"
    assert manager._execution_state == "idle"
    assert manager._active_procedure_run_id == ""
    assert manager._completion_terminate_started is False


def test_stop_interrupting_start_still_resets_without_reopening_start() -> None:
    manager = _terminal_manager()
    manager._operation_lock = threading.Lock()
    manager._operation_cancel = threading.Event()
    manager._latest_state = _terminal_state()
    calls: list[object] = []
    manager._publish_control = lambda command, **kwargs: calls.append(
        ("control", command, kwargs)
    )
    manager._executor_state_event = lambda: ("running", 88, time.monotonic())
    manager._command_executor = lambda command: calls.append(command) or (True, "terminated")
    manager._wait_for_executor_idle = lambda **kwargs: calls.append(
        ("settled", kwargs)
    ) or True
    manager._stop_digital_twin_to_halted = lambda: calls.append("halted") or True
    manager._publish_terminal_receipt = lambda **kwargs: calls.append(
        ("receipt", kwargs)
    ) or True
    manager._reset_digital_twin_to_idle = lambda **kwargs: calls.append(
        ("reset", kwargs)
    )

    assert (
        manager._interrupt_start_sequence("stop")
        == "start interrupted; simulation stopped and runtime reset to idle"
    )
    assert manager._operation_cancel.is_set()
    assert calls[0] == ("control", "stop", {})
    assert calls[1] == "terminate"
    assert calls[3] == "halted"
    assert calls[4][0] == "receipt"
    assert calls[5] == (
        "reset",
        {
            "expected_bundle": "thyroidectomy",
            "allow_cancelled_reset": True,
        },
    )
    assert manager._execution_state == "idle"


def test_finish_requests_twin_cleanup_and_is_immediately_idempotent() -> None:
    manager = _scenario_manager(running=True, state="running")
    manager._direct_request_pub = _Publisher()
    manager.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(to_msg=lambda: "finish-stamp")
    )

    assert manager._finish_sequence() == "completion cleanup requested"
    assert manager._running is True
    assert manager._execution_state == "finishing"
    assert len(manager._direct_request_pub.messages) == 1
    request = manager._direct_request_pub.messages[0]
    assert request.stamp == "finish-stamp"
    assert request.event_type == "request_procedure_completion"
    assert request.override is True
    assert manager._finish_sequence() == "completion cleanup already in progress"
    assert len(manager._direct_request_pub.messages) == 1


def test_finish_publishes_manager_owned_start_event_for_current_run() -> None:
    manager = _terminal_manager()
    manager._direct_request_pub = _Publisher()

    assert manager._finish_sequence() == "completion cleanup requested"

    assert len(manager._lifecycle_event_pub.messages) == 1
    event = json.loads(manager._lifecycle_event_pub.messages[0].data)
    assert event == {
        "schema": "taskplanner.simulation.lifecycle_event.v1",
        "event": "procedure_finishing",
        "procedure_run_id": "run-1",
    }


def test_control_finish_uses_the_graceful_cleanup_sequence() -> None:
    manager = _scenario_manager(running=True, state="running")
    manager._operation_name = ""
    calls: list[str] = []
    manager._run_sync = lambda name, _target: calls.append(name) or (
        True,
        "completion cleanup requested",
    )
    response = SimpleNamespace()

    manager._handle_control(
        SimpleNamespace(command="finish", start_phase_id=""),
        response,
    )

    assert calls == ["finish"]
    assert response.success is True
    assert response.message == "completion cleanup requested"
    assert response.running is True
    assert response.execution_state == "running"


def test_operational_override_is_admitted_only_while_running() -> None:
    manager = _scenario_manager(running=False, state="idle")
    response = SimpleNamespace()

    returned = manager._handle_operational_override(SimpleNamespace(), response)

    assert returned.success is False
    assert "not running" in returned.message


def test_debug_override_is_rejected_while_running_without_entering_override_route() -> None:
    manager = _scenario_manager(running=True, state="running")
    response = SimpleNamespace()
    manager._handle_override = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("debug override must not enter the publication route while running")
    )

    returned = manager._handle_debug_override(SimpleNamespace(), response)

    assert returned.success is False
    assert "pause or stop" in returned.message


def test_stale_pre_terminate_executor_snapshot_is_not_settlement() -> None:
    manager = _terminal_manager()
    manager._executor_state_event = lambda: ("halted", 9, time.monotonic())

    assert not manager._wait_for_executor_idle(
        timeout_sec=0.04,
        after_generation=9,
    )

    manager._executor_state_event = lambda: ("halted", 10, time.monotonic())
    assert manager._wait_for_executor_idle(
        timeout_sec=0.04,
        after_generation=9,
    )
