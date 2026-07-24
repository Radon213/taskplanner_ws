from __future__ import annotations

import threading
import sys
import types
from types import SimpleNamespace

try:
    from btops_interfaces import srv as _btops_srv  # noqa: F401
except ModuleNotFoundError:
    btops_package = types.ModuleType("btops_interfaces")
    btops_srv = types.ModuleType("btops_interfaces.srv")
    btops_srv.CommandExecutor = type("CommandExecutor", (), {})
    btops_srv.GetRuntimeState = type("GetRuntimeState", (), {})
    btops_srv.StartBehavior = type("StartBehavior", (), {})
    btops_package.srv = btops_srv
    sys.modules["btops_interfaces"] = btops_package
    sys.modules["btops_interfaces.srv"] = btops_srv

from simulation_runtime.simulation_manager import SimulationManagerNode


class _Logger:
    def warn(self, _message: str) -> None:
        pass


def _manager_for_start_gate(events: list[str]) -> SimulationManagerNode:
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._operation_lock = threading.Lock()
    manager._operation_cancel = threading.Event()
    manager._operation_name = "start"
    manager._running = False
    manager._execution_state = "starting"
    manager._bundle_dirty = True
    manager._publish_control = lambda command: events.append(command)
    manager._command_executor = lambda _command: (True, "")
    manager._wait_for_executor_idle = lambda timeout_sec=0.0: True
    manager._set_idle_state = lambda: (
        setattr(manager, "_running", False),
        setattr(manager, "_execution_state", "idle"),
    )
    manager.get_logger = lambda: _Logger()
    return manager


def test_start_actor_commit_and_reset_have_no_reset_then_restart_order():
    for _ in range(50):
        events: list[str] = []
        manager = _manager_for_start_gate(events)
        barrier = threading.Barrier(3)

        def commit() -> None:
            barrier.wait()
            try:
                manager._commit_start_actors("")
            except RuntimeError:
                pass

        def reset() -> None:
            barrier.wait()
            manager._interrupt_start_sequence("reset")

        commit_thread = threading.Thread(target=commit)
        reset_thread = threading.Thread(target=reset)
        commit_thread.start()
        reset_thread.start()
        barrier.wait()
        commit_thread.join(timeout=2.0)
        reset_thread.join(timeout=2.0)

        assert not commit_thread.is_alive()
        assert not reset_thread.is_alive()
        reset_positions = [index for index, value in enumerate(events) if value == "reset"]
        actor_positions = [
            index for index, value in enumerate(events) if value.startswith("start_actors")
        ]
        assert reset_positions
        if actor_positions:
            assert max(actor_positions) < min(reset_positions), events
        assert manager._running is False
        assert manager._execution_state == "idle"


def test_bundle_switch_quiesces_old_runtime_before_spec_change_and_restart():
    events: list[str] = []
    manager = _manager_for_start_gate(events)
    manager._operation_name = ""
    manager._running = True
    manager._execution_state = "running"
    manager._active_bundle = "thyroidectomy"
    manager._active_spec_dir = "/specs/thyroidectomy"
    old_spec = SimpleNamespace()
    new_spec = SimpleNamespace()
    manager._active_spec = old_spec
    manager._load_spec_for_bundle = lambda bundle: (
        f"/specs/{bundle}",
        new_spec,
    )
    manager._quiesce_runtime_for_bundle_change = lambda: events.append("quiesce-old")
    manager._set_spec_dir_on_runtime = lambda spec_dir: events.append(
        f"set-spec:{spec_dir}"
    )
    manager._start_sequence = lambda prepare_executor=False: (
        events.append("restart-new") or "new bundle running"
    )

    request = SimpleNamespace(bundle_name="nephrectomy", restart_if_running=True)
    response = SimpleNamespace()
    result = manager._handle_select_bundle(request, response)

    assert result.success is True
    assert result.active_bundle == "nephrectomy"
    assert events == [
        "quiesce-old",
        "set-spec:/specs/nephrectomy",
        "restart-new",
    ]
    assert manager._operation_name == ""
