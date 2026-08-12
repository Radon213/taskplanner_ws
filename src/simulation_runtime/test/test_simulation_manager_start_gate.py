from __future__ import annotations

import threading
import sys
import types
from types import SimpleNamespace

from procedure_spec import get_default_spec_dir, load_bundle

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

from simulation_runtime.simulation_manager import (
    RETRYABLE_START_ERROR_MARKERS,
    SimulationManagerNode,
    external_robot_contract_for_spec,
)


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


def _procedure_spec(procedure_id: str, operation: str = ""):
    groups = []
    if operation:
        groups.append(
            SimpleNamespace(
                enabled=True,
                allowed_operations=[operation],
            )
        )
    return SimpleNamespace(
        procedure_id=procedure_id,
        bed_robot_arm_groups=SimpleNamespace(groups=groups),
    )


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
    old_spec = _procedure_spec("thyroidectomy", "change_end_effector")
    new_spec = _procedure_spec("thyroidectomy_demo", "change_end_effector")
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

    request = SimpleNamespace(
        bundle_name="thyroidectomy_demo",
        restart_if_running=True,
    )
    response = SimpleNamespace()
    result = manager._handle_select_bundle(request, response)

    assert result.success is True
    assert result.active_bundle == "thyroidectomy_demo"
    assert events == [
        "quiesce-old",
        "set-spec:/specs/thyroidectomy_demo",
        "restart-new",
    ]
    assert manager._operation_name == ""


def test_bundle_switch_quiescence_accepts_launch_time_bundle_before_spec_update():
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    events: list[str] = []
    manager._reset_digital_twin_to_idle = lambda *, expected_bundle=None: events.append(
        f"reset:{expected_bundle!r}"
    )
    manager._prepare_executor_for_restart = lambda: events.append("executor-idle")

    manager._quiesce_runtime_for_bundle_change()

    assert events == ["reset:''", "executor-idle"]


def test_external_start_rejects_failed_integration_preflight():
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._require_integration_preflight = True
    manager._active_bundle = "thyroidectomy"
    manager._active_spec = _procedure_spec(
        "thyroidectomy", "change_end_effector"
    )
    manager._integration_preflight_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True,
        call_async=lambda _request: object(),
    )
    manager._configure_integration_preflight = lambda *_args, **_kwargs: None
    manager._operation_cancel = threading.Event()
    manager._integration_preflight_timeout_sec = 0.01
    manager._wait_future = lambda _future, timeout_sec: SimpleNamespace(
        success=False,
        message="integration not ready: skill_action_server",
    )

    try:
        manager._check_integration_preflight()
    except RuntimeError as exc:
        assert str(exc) == "integration not ready: skill_action_server"
    else:
        raise AssertionError("preflight failure did not block the start")


def test_external_start_accepts_ready_integration_preflight():
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._require_integration_preflight = True
    manager._active_bundle = "thyroidectomy"
    manager._active_spec = _procedure_spec(
        "thyroidectomy", "change_end_effector"
    )
    manager._integration_preflight_client = SimpleNamespace(
        wait_for_service=lambda timeout_sec: True,
        call_async=lambda _request: object(),
    )
    manager._configure_integration_preflight = lambda *_args, **_kwargs: None
    manager._operation_cancel = threading.Event()
    manager._wait_future = lambda _future, timeout_sec: SimpleNamespace(
        success=True,
        message="integration ready",
    )

    manager._check_integration_preflight()


def test_external_robot_contract_is_derived_from_loaded_spec() -> None:
    thyroid = external_robot_contract_for_spec(
        _procedure_spec("thyroidectomy_demo", "change_end_effector")
    )
    kidney = external_robot_contract_for_spec(
        _procedure_spec("nephrectomy", "retraction")
    )
    no_bed_robot = external_robot_contract_for_spec(
        _procedure_spec("inguinal_hernia_repair")
    )

    assert (
        thyroid.procedure_type,
        thyroid.require_tool_change_service,
        thyroid.require_retraction_adjustment_server,
        thyroid.require_bed_robot_arm_status,
    ) == ("thyroidectomy", True, False, True)
    assert (
        kidney.procedure_type,
        kidney.require_tool_change_service,
        kidney.require_retraction_adjustment_server,
        kidney.require_bed_robot_arm_status,
    ) == ("nephrectomy", False, True, True)
    assert no_bed_robot.procedure_type == ""
    assert no_bed_robot.require_bed_robot_arm_status is False


def test_external_robot_contract_accepts_public_procedure_spec_api() -> None:
    spec = load_bundle(get_default_spec_dir())

    contract = external_robot_contract_for_spec(spec)

    assert contract == external_robot_contract_for_spec(
        _procedure_spec("thyroidectomy", "change_end_effector")
    )


def test_bundle_switch_rejects_external_contract_change_before_quiescence() -> None:
    events: list[str] = []
    manager = _manager_for_start_gate(events)
    manager._operation_name = ""
    manager._running = False
    manager._execution_state = "idle"
    manager._active_bundle = "thyroidectomy"
    manager._active_spec_dir = "/specs/thyroidectomy"
    manager._active_spec = _procedure_spec(
        "thyroidectomy", "change_end_effector"
    )
    manager._runtime_external_robot_contract = external_robot_contract_for_spec(
        manager._active_spec
    )
    manager._load_spec_for_bundle = lambda _bundle: (
        "/specs/nephrectomy",
        _procedure_spec("nephrectomy", "retraction"),
    )
    manager._quiesce_runtime_for_bundle_change = lambda: events.append(
        "unsafe-quiesce"
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(bundle_name="nephrectomy", restart_if_running=True),
        SimpleNamespace(),
    )

    assert result.success is False
    assert "restart the runtime with default_bundle=nephrectomy" in result.message
    assert result.active_bundle == "thyroidectomy"
    assert result.spec_dir == "/specs/thyroidectomy"
    assert events == []


def test_same_contract_bundle_switch_closes_then_reopens_preflight() -> None:
    events: list[str] = []
    manager = _manager_for_start_gate(events)
    manager._operation_name = ""
    manager._running = True
    manager._execution_state = "running"
    manager._active_bundle = "thyroidectomy"
    manager._active_spec_dir = "/specs/thyroidectomy"
    manager._active_spec = _procedure_spec(
        "thyroidectomy", "change_end_effector"
    )
    manager._runtime_external_robot_contract = external_robot_contract_for_spec(
        manager._active_spec
    )
    target_spec = _procedure_spec(
        "thyroidectomy_demo", "change_end_effector"
    )
    manager._load_spec_for_bundle = lambda _bundle: (
        "/specs/thyroidectomy_demo",
        target_spec,
    )
    manager._configure_integration_preflight = (
        lambda bundle, _spec, *, transitioning: events.append(
            f"preflight:{bundle}:{transitioning}"
        )
    )
    manager._quiesce_runtime_for_bundle_change = lambda: events.append("quiesce")
    manager._set_spec_dir_on_runtime = lambda _spec_dir: events.append("set-spec")
    manager._start_sequence = lambda prepare_executor=False: (
        events.append("restart") or "running"
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy_demo",
            restart_if_running=True,
        ),
        SimpleNamespace(),
    )

    assert result.success is True
    assert result.active_bundle == "thyroidectomy_demo"
    assert events == [
        "preflight:thyroidectomy:True",
        "quiesce",
        "set-spec",
        "preflight:thyroidectomy_demo:False",
        "restart",
    ]


def test_empty_node_manifest_catalog_is_a_retryable_start_error():
    assert "node_manifest_identities is empty" in RETRYABLE_START_ERROR_MARKERS


def test_duplicate_transport_controls_are_idempotent_while_in_progress():
    for command in ("pause", "resume", "reset", "stop"):
        manager = SimulationManagerNode.__new__(SimulationManagerNode)
        manager._operation_name = command
        manager._running = command in {"pause", "resume"}
        manager._execution_state = {
            "pause": "running",
            "resume": "paused",
            "reset": "resetting",
            "stop": "stopping",
        }[command]

        result = manager._handle_control(
            SimpleNamespace(command=command, start_phase_id=""),
            SimpleNamespace(),
        )

        assert result.success is True
        assert result.message == f"{command} already in progress"


def _instrument_state(
    instance_id: str,
    *,
    location_id: str,
    location_type: str,
    lifecycle_stage: str,
    home_location_id: str,
    home_location_type: str,
):
    return SimpleNamespace(
        instance_id=instance_id,
        location_id=location_id,
        location_type=location_type,
        lifecycle_stage=lifecycle_stage,
        home_location_id=home_location_id,
        home_location_type=home_location_type,
    )


def test_start_layout_accepts_configured_deployed_instruments():
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._active_spec = SimpleNamespace(
        get_initial_instrument_states=lambda: [
            SimpleNamespace(
                instance_id="T03#1",
                location_id="field_region_procedure",
                lifecycle_stage="surgeon_owned",
            ),
            SimpleNamespace(
                instance_id="T03#2",
                location_id="field_region_procedure",
                lifecycle_stage="surgeon_owned",
            ),
        ]
    )
    state = SimpleNamespace(
        instrument_states=[
            _instrument_state(
                "T01#1",
                location_id="main_tray_slot_1",
                location_type="tray_slot",
                lifecycle_stage="home_rack",
                home_location_id="main_tray_slot_1",
                home_location_type="tray_slot",
            ),
            _instrument_state(
                "T03#1",
                location_id="field_region_procedure",
                location_type="surgical_field",
                lifecycle_stage="surgeon_owned",
                home_location_id="main_tray_slot_3",
                home_location_type="tray_slot",
            ),
            _instrument_state(
                "T03#2",
                location_id="field_region_procedure",
                location_type="surgical_field",
                lifecycle_stage="surgeon_owned",
                home_location_id="main_tray_slot_3",
                home_location_type="tray_slot",
            ),
        ]
    )

    assert manager._all_instruments_at_initial_layout(state) is True


def test_start_layout_rejects_missing_or_misplaced_configured_instrument():
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._active_spec = SimpleNamespace(
        get_initial_instrument_states=lambda: [
            SimpleNamespace(
                instance_id="T03#1",
                location_id="field_region_procedure",
                lifecycle_stage="surgeon_owned",
            ),
            SimpleNamespace(
                instance_id="T03#2",
                location_id="field_region_procedure",
                lifecycle_stage="surgeon_owned",
            ),
        ]
    )
    state = SimpleNamespace(
        instrument_states=[
            _instrument_state(
                "T03#1",
                location_id="main_tray_slot_3",
                location_type="tray_slot",
                lifecycle_stage="home_rack",
                home_location_id="main_tray_slot_3",
                home_location_type="tray_slot",
            ),
        ]
    )

    assert manager._all_instruments_at_initial_layout(state) is False
