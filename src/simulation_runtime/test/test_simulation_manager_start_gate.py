from __future__ import annotations

import threading
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from procedure_spec import get_default_spec_dir, load_bundle

try:
    from btops_interfaces import srv as _btops_srv  # noqa: F401
except ModuleNotFoundError:
    btops_package = types.ModuleType("btops_interfaces")
    btops_srv = types.ModuleType("btops_interfaces.srv")
    btops_srv.CommandExecutor = type("CommandExecutor", (), {})
    btops_srv.GetRuntimeState = type("GetRuntimeState", (), {})
    btops_srv.StartBehavior = type(
        "StartBehavior",
        (),
        {"Request": type("StartBehaviorRequest", (), {})},
    )
    btops_package.srv = btops_srv
    sys.modules["btops_interfaces"] = btops_package
    sys.modules["btops_interfaces.srv"] = btops_srv

from simulation_runtime.simulation_manager import (
    RETRYABLE_START_ERROR_MARKERS,
    SimulationManagerNode,
    TRANSITION_PROTOCOL_MARKER,
    compute_bundle_config_revision,
    external_robot_contract_for_spec,
    validate_bundle_name,
)


class _Logger:
    def info(self, _message: str) -> None:
        pass

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
    manager._active_config_revision = "sha256:thyroidectomy"
    manager._bundle_config_revision = lambda spec_dir: (
        f"sha256:{str(spec_dir).rstrip('/').rsplit('/', 1)[-1]}"
    )
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


def test_bundle_switch_does_not_interrupt_running_runtime_even_when_restart_requested():
    events: list[str] = []
    manager = _manager_for_start_gate(events)
    manager._operation_name = ""
    manager._running = True
    manager._execution_state = "running"
    manager._active_bundle = "thyroidectomy"
    manager._active_spec_dir = "/specs/thyroidectomy"
    old_spec = _procedure_spec("thyroidectomy", "change_end_effector")
    new_spec = _procedure_spec("thyroidectomy_demo", "retraction")
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

    assert result.success is False
    assert result.active_bundle == "thyroidectomy"
    assert result.applied is False
    assert result.disposition == "deferred"
    assert "not queued" in result.message
    assert events == []
    assert manager._operation_name == ""


def test_bundle_switch_does_not_interrupt_pending_start() -> None:
    events: list[str] = []
    manager = _manager_for_start_gate(events)
    manager._running = False
    manager._execution_state = "idle"
    manager._active_bundle = "thyroidectomy"
    manager._active_spec_dir = "/specs/thyroidectomy"
    manager._active_spec = _procedure_spec(
        "thyroidectomy", "change_end_effector"
    )
    manager._load_spec_for_bundle = lambda bundle: (
        f"/specs/{bundle}",
        _procedure_spec("thyroidectomy_demo", "retraction"),
    )
    manager._interrupt_start_sequence = lambda _command: (_ for _ in ()).throw(
        AssertionError("bundle selection must not interrupt start")
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy_demo",
            restart_if_running=True,
        ),
        SimpleNamespace(),
    )

    assert result.success is False
    assert result.applied is False
    assert result.disposition == "deferred"
    assert "not queued" in result.message
    assert manager._operation_name == "start"
    assert events == []


def test_bundle_switch_quiescence_accepts_launch_time_bundle_before_spec_update():
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    events: list[str] = []
    manager._reset_digital_twin_to_idle = lambda *, expected_bundle=None: events.append(
        f"reset:{expected_bundle!r}"
    )
    manager._prepare_executor_for_restart = lambda: events.append("executor-idle")

    manager._quiesce_runtime_for_bundle_change()

    assert events == ["reset:''", "executor-idle"]


def test_bundle_config_revision_tracks_yaml_content_not_mtime(tmp_path) -> None:
    spec_root = tmp_path / "specs"
    bundle_dir = spec_root / "procedure_a"
    bundle_dir.mkdir(parents=True)
    shared_catalog = spec_root / "display_catalog.yaml"
    procedure = bundle_dir / "procedure.yaml"
    ignored = bundle_dir / "notes.txt"
    shared_catalog.write_text("tools: {}\n", encoding="utf-8")
    procedure.write_text("procedure_id: procedure_a\n", encoding="utf-8")
    ignored.write_text("first\n", encoding="utf-8")

    original = compute_bundle_config_revision(bundle_dir)
    ignored.write_text("second\n", encoding="utf-8")
    assert compute_bundle_config_revision(bundle_dir) == original

    shared_catalog.write_text("tools:\n  changed: true\n", encoding="utf-8")
    assert compute_bundle_config_revision(bundle_dir) != original

    shared_catalog.write_text("tools: {}\n", encoding="utf-8")
    procedure.write_text("procedure_id: procedure_b\n", encoding="utf-8")
    assert compute_bundle_config_revision(bundle_dir) != original


def test_runtime_spec_transaction_updates_present_public_gateway() -> None:
    updates: dict[str, list[str]] = {}

    class _ReadyParameterClient:
        def __init__(self, name: str) -> None:
            self.name = name
            updates[name] = []

        def services_are_ready(self) -> bool:
            return True

        def wait_for_services(self, *, timeout_sec: float) -> bool:
            return True

        def set_parameters(self, parameters):
            updates[self.name].append(str(parameters[0].value))
            return SimpleNamespace(
                done=lambda: True,
                result=lambda: SimpleNamespace(
                    results=[SimpleNamespace(successful=True, reason="")]
                ),
            )

    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._require_integration_preflight = False
    manager._parameter_clients = {
        "/or_digital_twin": _ReadyParameterClient("/or_digital_twin"),
        "/surgical_interop_gateway": _ReadyParameterClient(
            "/surgical_interop_gateway"
        ),
    }
    manager.get_logger = lambda: _Logger()

    updated = manager._set_spec_dir_on_runtime(Path("/specs/thyroidectomy"))

    assert updated == {"/or_digital_twin", "/surgical_interop_gateway"}
    assert updates["/or_digital_twin"] == ["/specs/thyroidectomy"]
    assert updates["/surgical_interop_gateway"] == ["/specs/thyroidectomy"]


def test_present_public_gateway_rejection_rolls_back_spec_transaction() -> None:
    updates: dict[str, list[str]] = {}

    class _ParameterClient:
        def __init__(self, name: str, *, reject: bool = False) -> None:
            self.name = name
            self.reject = reject
            updates[name] = []

        def services_are_ready(self) -> bool:
            return True

        def wait_for_services(self, *, timeout_sec: float) -> bool:
            return True

        def set_parameters(self, parameters):
            updates[self.name].append(str(parameters[0].value))
            result = SimpleNamespace(
                successful=not self.reject,
                reason="gateway rejected active reload" if self.reject else "",
            )
            return SimpleNamespace(
                done=lambda: True,
                result=lambda: SimpleNamespace(results=[result]),
            )

    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._require_integration_preflight = False
    manager._parameter_clients = {
        "/or_digital_twin": _ParameterClient("/or_digital_twin"),
        "/surgical_interop_gateway": _ParameterClient(
            "/surgical_interop_gateway",
            reject=True,
        ),
    }
    manager.get_logger = lambda: _Logger()

    with pytest.raises(RuntimeError, match="gateway rejected active reload"):
        manager._set_spec_dir_on_runtime(
            Path("/specs/thyroidectomy_demo"),
            rollback_spec_dir=Path("/specs/thyroidectomy"),
        )

    assert updates["/or_digital_twin"] == [
        "/specs/thyroidectomy_demo",
        "/specs/thyroidectomy",
    ]
    assert updates["/surgical_interop_gateway"] == [
        "/specs/thyroidectomy_demo"
    ]


@pytest.mark.parametrize(
    "participant_name",
    ["/voice_command_resolver", "/surgical_interop_execution_bridge"],
)
def test_non_live_present_participant_rejection_rolls_back_spec_transaction(
    participant_name: str,
) -> None:
    updates: dict[str, list[str]] = {}

    class _ParameterClient:
        def __init__(self, name: str, *, reject: bool = False) -> None:
            self.name = name
            self.reject = reject
            updates[name] = []

        def services_are_ready(self) -> bool:
            return True

        def wait_for_services(self, *, timeout_sec: float) -> bool:
            return True

        def set_parameters(self, parameters):
            updates[self.name].append(str(parameters[0].value))
            result = SimpleNamespace(
                successful=not self.reject,
                reason="present participant rejected revision" if self.reject else "",
            )
            return SimpleNamespace(
                done=lambda: True,
                result=lambda: SimpleNamespace(results=[result]),
            )

    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._require_integration_preflight = False
    manager._parameter_clients = {
        "/or_digital_twin": _ParameterClient("/or_digital_twin"),
        participant_name: _ParameterClient(participant_name, reject=True),
    }
    manager.get_logger = lambda: _Logger()

    with pytest.raises(RuntimeError, match="present participant rejected revision"):
        manager._set_spec_dir_on_runtime(
            Path("/specs/thyroidectomy_demo"),
            rollback_spec_dir=Path("/specs/thyroidectomy"),
        )

    assert updates["/or_digital_twin"] == [
        "/specs/thyroidectomy_demo",
        "/specs/thyroidectomy",
    ]
    assert updates[participant_name] == ["/specs/thyroidectomy_demo"]


def test_bundle_name_accepts_simple_ids_and_rejects_path_syntax() -> None:
    assert (
        validate_bundle_name("thyroidectomy_demo.v2-1")
        == "thyroidectomy_demo.v2-1"
    )

    for value in ("..", "../outside", "/tmp/outside", "nested/name", ".hidden", "v1..2"):
        with pytest.raises(ValueError, match="simple identifier"):
            validate_bundle_name(value)


def test_bundle_loader_rejects_symlink_escape_from_spec_root(tmp_path) -> None:
    spec_root = tmp_path / "specs"
    outside = tmp_path / "outside"
    spec_root.mkdir()
    outside.mkdir()
    (spec_root / "escape").symlink_to(outside, target_is_directory=True)
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._spec_root = spec_root

    with pytest.raises(ValueError, match="outside the configured spec root"):
        manager._load_spec_for_bundle("escape")


def _manager_for_same_bundle_revision(
    events: list[str],
    *,
    state: str,
    running: bool,
) -> SimulationManagerNode:
    manager = _manager_for_start_gate(events)
    manager._operation_name = ""
    manager._running = running
    manager._execution_state = state
    manager._active_bundle = "thyroidectomy"
    manager._active_spec_dir = "/specs/thyroidectomy"
    manager._active_spec = _procedure_spec(
        "thyroidectomy", "change_end_effector"
    )
    manager._active_config_revision = "sha256:old"
    manager._runtime_external_robot_contract = external_robot_contract_for_spec(
        manager._active_spec
    )
    manager._load_spec_for_bundle = lambda _bundle: (
        "/specs/thyroidectomy",
        _procedure_spec("thyroidectomy", "change_end_effector"),
    )
    manager._bundle_config_revision = lambda _spec_dir: "sha256:new"
    manager._configure_integration_preflight = (
        lambda bundle, _spec, *, transitioning: events.append(
            f"preflight:{bundle}:{transitioning}"
        )
    )
    manager._quiesce_runtime_for_bundle_change = lambda: events.append(
        "quiesce"
    )
    manager._set_spec_dir_on_runtime = lambda spec_dir, **_kwargs: events.append(
        f"set-spec:{spec_dir}"
    )
    manager._reset_digital_twin_to_idle = lambda **_kwargs: events.append(
        "reset"
    )
    return manager


def test_bundle_service_rejects_path_traversal_before_candidate_load() -> None:
    events: list[str] = []
    manager = _manager_for_same_bundle_revision(
        events,
        state="halted",
        running=False,
    )
    manager._load_spec_for_bundle = lambda _bundle: (_ for _ in ()).throw(
        AssertionError("path syntax must be rejected before loading")
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(bundle_name="../outside", restart_if_running=False),
        SimpleNamespace(),
    )

    assert result.success is False
    assert result.applied is False
    assert result.disposition == "rejected"
    assert "path traversal is not allowed" in result.message
    assert events == []


def test_same_bundle_preview_is_read_only_while_running() -> None:
    events: list[str] = []
    manager = _manager_for_same_bundle_revision(
        events,
        state="running",
        running=True,
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy",
            restart_if_running=False,
            preview_only=True,
            reload_if_changed=False,
        ),
        SimpleNamespace(),
    )

    assert result.success is True
    assert result.changed is True
    assert result.applied is False
    assert result.active_config_revision == "sha256:old"
    assert result.candidate_config_revision == "sha256:new"
    assert result.disposition == "preview_change_available"
    assert manager._running is True
    assert manager._execution_state == "running"
    assert events == []


def test_apply_rejects_a_candidate_that_changed_since_preview() -> None:
    events: list[str] = []
    manager = _manager_for_same_bundle_revision(
        events,
        state="halted",
        running=False,
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy",
            restart_if_running=False,
            preview_only=False,
            reload_if_changed=True,
            expected_candidate_revision="sha256:previewed",
        ),
        SimpleNamespace(),
    )

    assert result.success is False
    assert result.applied is False
    assert result.active_config_revision == "sha256:old"
    assert result.candidate_config_revision == "sha256:new"
    assert result.disposition == "revision_changed"
    assert "preview" in result.message
    assert events == []


def test_same_bundle_unchanged_reload_is_noop_even_while_running() -> None:
    events: list[str] = []
    manager = _manager_for_same_bundle_revision(
        events,
        state="running",
        running=True,
    )
    manager._bundle_config_revision = lambda _spec_dir: "sha256:old"

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy",
            restart_if_running=True,
            preview_only=False,
            reload_if_changed=True,
        ),
        SimpleNamespace(),
    )

    assert result.success is True
    assert result.changed is False
    assert result.applied is False
    assert result.disposition == "unchanged"
    assert events == []


def test_legacy_same_bundle_selection_reports_change_without_applying() -> None:
    events: list[str] = []
    manager = _manager_for_same_bundle_revision(
        events,
        state="idle",
        running=False,
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy",
            restart_if_running=False,
        ),
        SimpleNamespace(),
    )

    assert result.success is True
    assert result.changed is True
    assert result.applied is False
    assert result.disposition == "change_available"
    assert events == []


@pytest.mark.parametrize(
    ("state", "running"),
    [("running", True), ("paused", True)],
)
def test_same_bundle_reload_is_deferred_without_resetting_active_state(
    state: str,
    running: bool,
) -> None:
    events: list[str] = []
    manager = _manager_for_same_bundle_revision(
        events,
        state=state,
        running=running,
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy",
            restart_if_running=True,
            preview_only=False,
            reload_if_changed=True,
        ),
        SimpleNamespace(),
    )

    assert result.success is False
    assert result.changed is True
    assert result.applied is False
    assert result.disposition == "deferred"
    assert "not queued" in result.message
    assert manager._running is running
    assert manager._execution_state == state
    assert manager._active_config_revision == "sha256:old"
    assert events == []


def test_same_bundle_reload_applies_new_revision_without_runtime_restart() -> None:
    events: list[str] = []
    manager = _manager_for_same_bundle_revision(
        events,
        state="halted",
        running=False,
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy",
            restart_if_running=False,
            preview_only=False,
            reload_if_changed=True,
        ),
        SimpleNamespace(),
    )

    assert result.success is True
    assert result.changed is True
    assert result.applied is True
    assert result.disposition == "applied"
    assert result.active_config_revision == "sha256:new"
    assert result.candidate_config_revision == "sha256:new"
    assert manager._active_config_revision == "sha256:new"
    assert "without a runtime restart" in result.message
    assert events == [
        "preflight:thyroidectomy:True",
        "quiesce",
        "preflight:thyroidectomy:True",
        "set-spec:/specs/thyroidectomy",
        "reset",
        "preflight:thyroidectomy:False",
    ]


def test_revision_barrier_rejects_edit_after_candidate_load_before_apply() -> None:
    events: list[str] = []
    manager = _manager_for_same_bundle_revision(
        events,
        state="halted",
        running=False,
    )
    revisions = iter(("sha256:candidate", "sha256:edited"))
    manager._bundle_config_revision = lambda _spec_dir: next(revisions)

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy",
            restart_if_running=False,
            preview_only=False,
            reload_if_changed=True,
        ),
        SimpleNamespace(),
    )

    assert result.success is False
    assert result.applied is False
    assert result.disposition == "rejected"
    assert "before participant apply" in result.message
    assert manager._active_config_revision == "sha256:old"
    assert events == [
        "preflight:thyroidectomy:True",
        "quiesce",
        "preflight:thyroidectomy:True",
    ]


def test_non_live_same_bundle_post_apply_race_blocks_restart_until_recovery() -> None:
    events: list[str] = []
    manager = _manager_for_same_bundle_revision(
        events,
        state="halted",
        running=False,
    )
    revisions = iter(
        ("sha256:candidate", "sha256:candidate", "sha256:edited")
    )
    manager._bundle_config_revision = lambda _spec_dir: next(revisions)

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy",
            restart_if_running=False,
            preview_only=False,
            reload_if_changed=True,
        ),
        SimpleNamespace(),
    )

    assert result.success is False
    assert result.applied is False
    assert "runtime recovery is required" in result.message
    assert manager._bundle_reload_recovery_required is True
    start = manager._handle_control(
        SimpleNamespace(command="start", start_phase_id=""),
        SimpleNamespace(),
    )
    assert start.success is False
    assert "configuration recovery is required" in start.message
    assert events == [
        "preflight:thyroidectomy:True",
        "quiesce",
        "preflight:thyroidectomy:True",
        "set-spec:/specs/thyroidectomy",
    ]


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
        _procedure_spec("thyroidectomy_demo", "retraction")
    )
    kidney = external_robot_contract_for_spec(
        _procedure_spec("nephrectomy", "retraction")
    )
    no_bed_robot = external_robot_contract_for_spec(
        _procedure_spec("inguinal_hernia_repair")
    )
    inguinal_demo = external_robot_contract_for_spec(
        _procedure_spec("inguinal_hernia_repair_demo", "retraction")
    )

    assert (
        thyroid.procedure_type,
        thyroid.require_retraction_service,
        thyroid.require_bed_robot_arm_status,
    ) == ("thyroidectomy", True, False)
    assert (
        kidney.procedure_type,
        kidney.require_retraction_service,
        kidney.require_bed_robot_arm_status,
    ) == ("nephrectomy", True, False)
    assert (
        inguinal_demo.procedure_type,
        inguinal_demo.require_retraction_service,
        inguinal_demo.require_bed_robot_arm_status,
        inguinal_demo.require_tool_handover_action_server,
    ) == ("inguinal_hernia_repair", True, False, False)
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


def test_paused_bundle_switch_requires_explicit_restart_intent() -> None:
    events: list[str] = []
    manager = _manager_for_start_gate(events)
    manager._operation_name = ""
    manager._running = True
    manager._execution_state = "paused"
    manager._active_bundle = "thyroidectomy"
    manager._active_spec_dir = "/specs/thyroidectomy"
    manager._active_spec = _procedure_spec(
        "thyroidectomy", "change_end_effector"
    )
    manager._load_spec_for_bundle = lambda _bundle: (
        "/specs/thyroidectomy_demo",
        _procedure_spec("thyroidectomy_demo", "retraction"),
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy_demo",
            restart_if_running=False,
        ),
        SimpleNamespace(),
    )

    assert result.success is False
    assert result.applied is False
    assert result.disposition == "deferred"
    assert "not queued" in result.message
    assert manager._active_bundle == "thyroidectomy"
    assert events == []


def test_paused_explicit_bundle_switch_restarts_new_scenario() -> None:
    events: list[str] = []
    manager = _manager_for_start_gate(events)
    manager._operation_name = ""
    manager._running = True
    manager._execution_state = "paused"
    manager._active_bundle = "thyroidectomy"
    manager._active_spec_dir = "/specs/thyroidectomy"
    manager._active_spec = _procedure_spec(
        "thyroidectomy", "change_end_effector"
    )
    manager._runtime_external_robot_contract = external_robot_contract_for_spec(
        manager._active_spec
    )
    target_spec = _procedure_spec(
        "thyroidectomy_demo", "retraction"
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
    manager._set_spec_dir_on_runtime = lambda _spec_dir, **_kwargs: events.append(
        "set-spec"
    )
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
    assert result.applied is True
    assert result.disposition == "applied"
    assert events == [
        "preflight:thyroidectomy:True",
        "quiesce",
        "preflight:thyroidectomy_demo:True",
        "set-spec",
        "restart",
        "preflight:thyroidectomy_demo:False",
    ]


def _manager_for_live_bundle_switch(events: list[str]) -> SimulationManagerNode:
    manager = _manager_for_transition_ready()
    manager._require_integration_preflight = True
    manager._active_bundle = "thyroidectomy_demo"
    manager._active_spec_dir = "/specs/thyroidectomy_demo"
    manager._active_config_revision = "sha256:thyroidectomy_demo"
    manager._bundle_config_revision = lambda spec_dir: (
        f"sha256:{str(spec_dir).rstrip('/').rsplit('/', 1)[-1]}"
    )
    manager._active_spec = _procedure_spec("thyroidectomy_demo", "retraction")
    manager._runtime_external_robot_contract = external_robot_contract_for_spec(
        manager._active_spec
    )
    manager._latest_state.robot_state = "idle"
    manager._latest_state.bed_robot_arm_groups = []
    manager._get_runtime_state_detail = lambda **_kwargs: (
        True,
        "terminated",
        "test executor is settled",
    )
    manager._load_spec_for_bundle = lambda bundle: (
        f"/specs/{bundle}",
        _procedure_spec("nephrectomy", "retraction"),
    )
    manager._configure_integration_preflight = (
        lambda bundle, _spec, *, transitioning: events.append(
            f"preflight:{bundle}:{transitioning}"
        )
    )
    manager._quiesce_runtime_for_bundle_change = lambda: events.append("quiesce")
    manager._set_spec_dir_on_runtime = lambda spec_dir, **kwargs: events.append(
        f"set-spec:{spec_dir}:{kwargs.get('rollback_spec_dir', '')}"
    )
    manager._reset_digital_twin_to_idle = lambda **_kwargs: events.append("reset")
    return manager


def test_live_cross_bundle_preview_is_read_only_while_running() -> None:
    events: list[str] = []
    manager = _manager_for_live_bundle_switch(events)
    manager._running = True
    manager._execution_state = "running"
    manager._latest_state.running = True
    manager._latest_state.execution_state = "running"

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="nephrectomy",
            restart_if_running=False,
            preview_only=True,
            reload_if_changed=False,
            expected_candidate_revision="",
        ),
        SimpleNamespace(),
    )

    assert result.success is True
    assert result.active_bundle == "thyroidectomy_demo"
    assert result.changed is True
    assert result.applied is False
    assert result.disposition == "preview_change_available"
    assert manager._running is True
    assert manager._execution_state == "running"
    assert events == []


def test_live_same_bundle_reload_failure_keeps_admission_closed() -> None:
    events: list[str] = []
    manager = _manager_for_live_bundle_switch(events)
    manager._active_config_revision = "sha256:old"
    manager._bundle_config_revision = lambda _spec_dir: "sha256:new"
    manager._load_spec_for_bundle = lambda _bundle: (
        "/specs/thyroidectomy_demo",
        _procedure_spec("thyroidectomy_demo", "retraction"),
    )

    def reject_partial_reload(spec_dir, **kwargs):
        events.append(
            f"set-spec:{spec_dir}:{kwargs.get('rollback_spec_dir')!r}"
        )
        raise RuntimeError("participant rejected reload")

    manager._set_spec_dir_on_runtime = reject_partial_reload

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy_demo",
            restart_if_running=False,
            preview_only=False,
            reload_if_changed=True,
        ),
        SimpleNamespace(),
    )

    assert result.success is False
    assert result.applied is False
    assert result.disposition == "rejected"
    assert "runtime recovery is required" in result.message
    assert manager._active_config_revision == "sha256:old"
    assert events == [
        "preflight:thyroidectomy_demo:True",
        "quiesce",
        "preflight:thyroidectomy_demo:True",
        "set-spec:/specs/thyroidectomy_demo:None",
        "preflight:thyroidectomy_demo:True",
    ]


def test_live_same_bundle_post_apply_revision_race_requires_recovery() -> None:
    events: list[str] = []
    manager = _manager_for_live_bundle_switch(events)
    manager._active_config_revision = "sha256:old"
    manager._load_spec_for_bundle = lambda _bundle: (
        "/specs/thyroidectomy_demo",
        _procedure_spec("thyroidectomy_demo", "retraction"),
    )
    revisions = iter(
        ("sha256:candidate", "sha256:candidate", "sha256:edited")
    )
    manager._bundle_config_revision = lambda _spec_dir: next(revisions)

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy_demo",
            restart_if_running=False,
            preview_only=False,
            reload_if_changed=True,
        ),
        SimpleNamespace(),
    )

    assert result.success is False
    assert result.applied is False
    assert result.disposition == "rejected"
    assert "runtime recovery is required" in result.message
    assert manager._active_config_revision == "sha256:old"
    assert events == [
        "preflight:thyroidectomy_demo:True",
        "quiesce",
        "preflight:thyroidectomy_demo:True",
        "set-spec:/specs/thyroidectomy_demo:None",
        "preflight:thyroidectomy_demo:True",
    ]


def test_live_bundle_switch_accepts_stopped_same_endpoint_shape_without_readiness() -> None:
    events: list[str] = []
    manager = _manager_for_live_bundle_switch(events)

    result = manager._handle_select_bundle(
        SimpleNamespace(bundle_name="nephrectomy", restart_if_running=False),
        SimpleNamespace(),
    )

    assert result.success is True
    assert result.active_bundle == "nephrectomy"
    assert result.spec_dir == "/specs/nephrectomy"
    assert events == [
        "preflight:thyroidectomy_demo:True",
        "quiesce",
        "preflight:nephrectomy:True",
        "set-spec:/specs/nephrectomy:/specs/thyroidectomy_demo",
        "reset",
        "preflight:nephrectomy:False",
    ]


def test_live_bundle_switch_accepts_required_endpoint_subset_and_preserves_capacity() -> None:
    events: list[str] = []
    manager = _manager_for_live_bundle_switch(events)
    launch_capacity = manager._runtime_external_robot_contract
    manager._load_spec_for_bundle = lambda bundle: (
        f"/specs/{bundle}",
        _procedure_spec("inguinal_hernia_repair_demo", "retraction"),
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="inguinal_hernia_repair_demo",
            restart_if_running=False,
        ),
        SimpleNamespace(),
    )

    assert result.success is True
    assert result.active_bundle == "inguinal_hernia_repair_demo"
    assert manager._runtime_external_robot_contract is launch_capacity
    assert manager._runtime_external_robot_contract.endpoint_shape == (
        True,
        True,
        False,
    )

    manager._load_spec_for_bundle = lambda bundle: (
        f"/specs/{bundle}",
        _procedure_spec("thyroidectomy_demo", "retraction"),
    )
    returned = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy_demo",
            restart_if_running=False,
        ),
        SimpleNamespace(),
    )

    assert returned.success is True
    assert returned.active_bundle == "thyroidectomy_demo"
    assert manager._runtime_external_robot_contract is launch_capacity


def test_live_bundle_switch_rejects_target_endpoint_missing_from_launch_capacity() -> None:
    events: list[str] = []
    manager = _manager_for_live_bundle_switch(events)
    hernia_spec = _procedure_spec(
        "inguinal_hernia_repair_demo",
        "retraction",
    )
    manager._active_bundle = "inguinal_hernia_repair_demo"
    manager._active_spec_dir = "/specs/inguinal_hernia_repair_demo"
    manager._active_spec = hernia_spec
    manager._runtime_external_robot_contract = external_robot_contract_for_spec(
        hernia_spec
    )
    manager._load_spec_for_bundle = lambda bundle: (
        f"/specs/{bundle}",
        _procedure_spec("thyroidectomy_demo", "retraction"),
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(
            bundle_name="thyroidectomy_demo",
            restart_if_running=False,
        ),
        SimpleNamespace(),
    )

    assert result.success is False
    assert "changes the launch-time endpoint shape" in result.message
    assert result.active_bundle == "inguinal_hernia_repair_demo"
    assert events == []


@pytest.mark.parametrize(
    ("state_overrides", "expected"),
    [
        ({"active_robot_task_id": "task-1"}, "a robot task is still active"),
        ({"cleaner_busy": True}, "cleaner activity is still pending"),
        ({"robot_state": "fault"}, "robot is faulted: fault"),
    ],
)
def test_live_bundle_switch_rejects_any_nonquiescent_or_faulted_state(
    state_overrides: dict[str, object], expected: str
) -> None:
    events: list[str] = []
    manager = _manager_for_live_bundle_switch(events)
    for key, value in state_overrides.items():
        setattr(manager._latest_state, key, value)
    manager._load_spec_for_bundle = lambda _bundle: (_ for _ in ()).throw(
        AssertionError("unsafe Live selection must not load a target spec")
    )

    result = manager._handle_select_bundle(
        SimpleNamespace(bundle_name="nephrectomy", restart_if_running=True),
        SimpleNamespace(),
    )

    assert result.success is False
    assert expected in result.message
    assert result.active_bundle == "thyroidectomy_demo"
    assert events == []


def test_live_resume_rechecks_preflight_before_publishing_any_resume() -> None:
    events: list[str] = []
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._running = True
    manager._execution_state = "paused"
    manager._check_integration_preflight = lambda: (_ for _ in ()).throw(
        RuntimeError("integration not ready: speech_input")
    )
    manager._publish_control = lambda command: events.append(f"control:{command}")
    manager._command_executor = lambda command: events.append(f"executor:{command}") or (True, "")

    try:
        manager._resume_sequence()
    except RuntimeError as exc:
        assert str(exc) == "integration not ready: speech_input"
    else:
        raise AssertionError("Live resume did not fail closed on preflight")

    assert events == []
    assert manager._running is True
    assert manager._execution_state == "paused"


def test_resume_admits_before_twin_then_executor() -> None:
    events: list[str] = []
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._running = True
    manager._execution_state = "paused"
    manager._check_integration_preflight = lambda: events.append("preflight")
    manager._publish_control = lambda command: events.append(f"control:{command}")
    manager._wait_for_simulation_state = lambda *_args, **_kwargs: events.append("twin")
    manager._command_executor = lambda command: events.append(f"executor:{command}") or (True, "resumed")
    manager._wait_for_executor_running = lambda **_kwargs: events.append("executor-running")
    manager.get_logger = lambda: _Logger()

    result = manager._resume_sequence()

    assert result == "resumed"
    assert events == [
        "preflight",
        "control:resume",
        "twin",
        "executor:resume",
        "executor-running",
    ]
    assert manager._running is True
    assert manager._execution_state == "running"


def test_empty_node_manifest_catalog_is_a_retryable_start_error():
    assert "node_manifest_identities is empty" in RETRYABLE_START_ERROR_MARKERS


def test_duplicate_transport_controls_are_idempotent_while_in_progress():
    for command in ("pause", "resume", "reset", "stop"):
        manager = SimulationManagerNode.__new__(SimulationManagerNode)
        manager._operation_lock = threading.Lock()
        manager._transition_reservation_until_monotonic = 0.0
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


def _manager_for_transition_ready() -> SimulationManagerNode:
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._operation_lock = threading.Lock()
    manager._operation_cancel = threading.Event()
    manager._operation_name = ""
    manager._completion_terminate_started = False
    manager._transition_reservation_ttl_sec = 75.0
    manager._transition_reservation_until_monotonic = 0.0
    manager._bundle_transition_in_progress = False
    manager._override_in_progress = False
    manager._operation_rejection_message = ""
    manager._executor_settled_confirmed = False
    manager._running = False
    manager._execution_state = "halted"
    manager._latest_state_lock = threading.Lock()
    manager._latest_state_received_monotonic = time.monotonic()
    manager._latest_state = SimpleNamespace(
        running=False,
        execution_state="halted",
        active_robot_task_id="",
        cleaner_busy=False,
        pending_transition_tools=[],
        active_recovery_tools=[],
    )
    return manager


def test_transition_ready_rejects_early_halted_while_stop_is_pending() -> None:
    manager = _manager_for_transition_ready()
    manager._operation_name = "stop"
    manager._get_runtime_state_detail = lambda **_kwargs: (
        True,
        "terminated",
        "ok",
    )

    ready, reason = manager._transition_ready_status()

    assert ready is False
    assert reason == "simulation operation is still pending: stop"


def test_transition_ready_rejects_unsettled_executor_and_active_action() -> None:
    manager = _manager_for_transition_ready()
    manager._get_runtime_state_detail = lambda **_kwargs: (
        True,
        "terminating",
        "ok",
    )
    ready, reason = manager._transition_ready_status()
    assert ready is False
    assert reason == "executor is not settled: terminating"

    manager._latest_state.active_robot_task_id = "task-in-flight"
    manager._get_runtime_state_detail = lambda **_kwargs: (
        True,
        "terminated",
        "ok",
    )
    ready, reason = manager._transition_ready_status()
    assert ready is False
    assert reason == "a robot task is still active"


def test_transition_ready_allows_only_settled_manager_executor_and_actions() -> None:
    manager = _manager_for_transition_ready()
    manager._get_runtime_state_detail = lambda **_kwargs: (
        True,
        "terminated",
        "ok",
    )

    ready, reason = manager._transition_ready_status()

    assert ready is True
    assert reason == (
        f"{TRANSITION_PROTOCOL_MARKER} transition ready; executor=terminated"
    )
    assert manager._executor_settled_confirmed is True


def test_transition_ready_no_snapshot_is_safe_only_before_activity() -> None:
    manager = _manager_for_transition_ready()
    manager._get_runtime_state_detail = lambda **_kwargs: (
        False,
        "unknown",
        "No runtime snapshot is available for 'tree_executor'.",
    )

    ready, _reason = manager._transition_ready_status()
    assert ready is False

    manager._executor_settled_confirmed = True
    ready, reason = manager._transition_ready_status()
    assert ready is True
    assert reason == (
        f"{TRANSITION_PROTOCOL_MARKER} "
        "transition ready; executor has no active session"
    )


def test_transition_state_receipt_accepts_exact_deadline_and_rejects_epsilon(
    monkeypatch,
) -> None:
    manager = _manager_for_transition_ready()
    manager._get_runtime_state_detail = lambda **_kwargs: (True, "terminated", "ok")
    manager._latest_state_received_monotonic = 97.0
    now = [100.0]
    monkeypatch.setattr(
        "simulation_runtime.simulation_manager.time.monotonic",
        lambda: now[0],
    )

    ready, _reason = manager._transition_ready_status()
    assert ready is True

    now[0] = 100.000001
    ready, reason = manager._transition_ready_status()
    assert ready is False
    assert reason == "digital twin state receipt is stale"


def test_stale_receipt_rejects_check_and_reserve_without_deadline(monkeypatch) -> None:
    manager = _manager_for_transition_ready()
    manager._get_runtime_state_detail = lambda **_kwargs: (True, "terminated", "ok")
    manager._latest_state_received_monotonic = 10.0
    monkeypatch.setattr(
        "simulation_runtime.simulation_manager.time.monotonic",
        lambda: 20.0,
    )

    checked = manager._handle_check_transition_ready(
        SimpleNamespace(), SimpleNamespace()
    )
    reserved = manager._handle_reserve_transition(
        SimpleNamespace(), SimpleNamespace()
    )

    assert checked.success is False
    assert reserved.success is False
    assert checked.message == "digital twin state receipt is stale"
    assert reserved.message == "digital twin state receipt is stale"
    assert manager._transition_reservation_until_monotonic == 0.0


def test_reserve_rechecks_active_digital_twin_after_executor_query() -> None:
    manager = _manager_for_transition_ready()

    def query(**_kwargs):
        manager._latest_state.running = True
        manager._latest_state.execution_state = "running"
        return True, "terminated", "ok"

    manager._get_runtime_state_detail = query
    reserved = manager._handle_reserve_transition(
        SimpleNamespace(), SimpleNamespace()
    )

    assert reserved.success is False
    assert reserved.message == "digital twin has not reached an inactive state"
    assert manager._transition_reservation_until_monotonic == 0.0


def test_check_rechecks_local_termination_gate_after_executor_query() -> None:
    manager = _manager_for_transition_ready()

    def query(**_kwargs):
        manager._completion_terminate_started = True
        return True, "terminated", "ok"

    manager._get_runtime_state_detail = query
    checked = manager._handle_check_transition_ready(
        SimpleNamespace(), SimpleNamespace()
    )

    assert checked.success is False
    assert checked.message == "completion-triggered executor termination is still pending"


def test_reserve_rechecks_stale_receipt_after_executor_query(monkeypatch) -> None:
    manager = _manager_for_transition_ready()
    now = [100.0]
    manager._latest_state_received_monotonic = 100.0
    monkeypatch.setattr(
        "simulation_runtime.simulation_manager.time.monotonic",
        lambda: now[0],
    )

    def query(**_kwargs):
        now[0] = 103.000001
        return True, "terminated", "ok"

    manager._get_runtime_state_detail = query
    reserved = manager._handle_reserve_transition(
        SimpleNamespace(), SimpleNamespace()
    )

    assert reserved.success is False
    assert reserved.message == "digital twin state receipt is stale"
    assert manager._transition_reservation_until_monotonic == 0.0


def test_identical_simulation_state_heartbeat_refreshes_receipt(monkeypatch) -> None:
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._latest_state_lock = threading.Lock()
    manager._latest_state = None
    manager._latest_state_generation = 0
    manager._latest_state_received_monotonic = 0.0
    heartbeat = SimpleNamespace(execution_state="halted")
    now = iter((10.0, 12.0))
    monkeypatch.setattr(
        "simulation_runtime.simulation_manager.time.monotonic",
        lambda: next(now),
    )

    manager._on_simulation_state(heartbeat)
    manager._on_simulation_state(heartbeat)

    assert manager._latest_state is heartbeat
    assert manager._latest_state_generation == 2
    assert manager._latest_state_received_monotonic == 12.0


def test_transition_reservation_is_idempotent_without_renewing_deadline() -> None:
    manager = _manager_for_transition_ready()
    manager._transition_ready_status_locked = lambda: (True, "ready")
    first = manager._handle_reserve_transition(SimpleNamespace(), SimpleNamespace())
    original_deadline = manager._transition_reservation_until_monotonic

    second = manager._handle_reserve_transition(SimpleNamespace(), SimpleNamespace())

    assert first.success is True
    assert second.success is True
    assert second.message == (
        f"{TRANSITION_PROTOCOL_MARKER} runtime transition is already reserved"
    )
    assert manager._transition_reservation_until_monotonic == original_deadline
    assert original_deadline >= time.monotonic() + 59.0


def test_transition_reservation_expires_lazily_and_check_is_read_only() -> None:
    manager = _manager_for_transition_ready()
    manager._transition_reservation_until_monotonic = time.monotonic() + 60.0

    ready, reason = manager._transition_ready_status()
    assert ready is False
    assert reason == "runtime mode transition is already reserved"

    manager._transition_reservation_until_monotonic = time.monotonic() - 0.01
    manager._get_runtime_state_detail = lambda **_kwargs: (True, "terminated", "ok")
    ready, reason = manager._transition_ready_status()
    assert ready is True
    assert reason == (
        f"{TRANSITION_PROTOCOL_MARKER} transition ready; executor=terminated"
    )
    assert manager._transition_reservation_until_monotonic == 0.0


def test_reserved_transition_blocks_mutations_without_state_changes() -> None:
    manager = _manager_for_transition_ready()
    manager._transition_reservation_until_monotonic = time.monotonic() + 60.0
    manager._bundle_dirty = True
    original = (manager._running, manager._execution_state, manager._bundle_dirty)

    for command in ("start", "resume", "pause", "reset"):
        response = manager._handle_control(
            SimpleNamespace(command=command, start_phase_id=""),
            SimpleNamespace(),
        )
        assert response.success is False
        assert "transition is reserved" in response.message
        assert (manager._running, manager._execution_state, manager._bundle_dirty) == original


def test_reserve_and_begin_operation_form_an_atomic_barrier() -> None:
    manager = _manager_for_transition_ready()
    manager._get_runtime_state_detail = lambda **_kwargs: (True, "terminated", "ok")

    reserved = manager._handle_reserve_transition(SimpleNamespace(), SimpleNamespace())
    assert reserved.success is True
    assert manager._begin_operation("start") is False
    assert manager._operation_name == ""

    manager._transition_reservation_until_monotonic = 0.0
    assert manager._begin_operation("start") is True
    rejected = manager._handle_reserve_transition(SimpleNamespace(), SimpleNamespace())
    assert rejected.success is False
    assert manager._transition_reservation_until_monotonic == 0.0


def test_reserved_transition_rejects_bundle_selection_and_override() -> None:
    manager = _manager_for_transition_ready()
    manager._transition_reservation_until_monotonic = time.monotonic() + 60.0
    manager._active_bundle = "thyroidectomy"
    manager._active_spec_dir = "/specs/thyroidectomy"

    bundle = manager._handle_select_bundle(
        SimpleNamespace(bundle_name="other", restart_if_running=False),
        SimpleNamespace(),
    )
    override = manager._handle_override(
        SimpleNamespace(),
        SimpleNamespace(),
    )

    assert bundle.success is False
    assert "bundle selection rejected" in bundle.message
    assert override.success is False
    assert "surgeon override rejected" in override.message


class _ImmediateFuture:
    def __init__(self, result) -> None:
        self._result = result

    def done(self) -> bool:
        return True

    def result(self):
        return self._result


class _StartClient:
    def __init__(self, response) -> None:
        self._response = response

    def wait_for_service(self, timeout_sec: float) -> bool:
        return True

    def call_async(self, _request):
        return _ImmediateFuture(self._response)


def _manager_for_start_response(response) -> SimulationManagerNode:
    manager = SimulationManagerNode.__new__(SimulationManagerNode)
    manager._start_client = _StartClient(response)
    manager._operation_cancel = threading.Event()
    manager._executor_name = "tree_executor"
    manager._tick_rate_hz = 0.1
    manager._groot2_port = 1667
    manager._executor_settled_confirmed = True
    manager._wait_for_executor_idle = lambda timeout_sec=0.0: True
    manager._wait_for_executor_running = lambda timeout_sec=0.0: False
    manager._command_executor = lambda _command: (True, "terminated")
    manager.get_logger = lambda: _Logger()
    return manager


def test_explicit_start_rejection_with_no_snapshot_restores_settled_state() -> None:
    manager = _manager_for_start_response(
        SimpleNamespace(success=False, message="start explicitly rejected")
    )
    manager._get_runtime_state_detail = lambda **_kwargs: (
        False,
        "unknown",
        "No runtime snapshot is available for 'tree_executor'.",
    )

    success, _message = manager._start_behavior(clear_blackboard=True)

    assert success is False
    assert manager._executor_settled_confirmed is True


def test_missing_start_response_keeps_executor_activity_fail_closed(monkeypatch) -> None:
    manager = _manager_for_start_response(None)
    monkeypatch.setattr("simulation_runtime.simulation_manager.time.sleep", lambda _sec: None)

    success, _message = manager._start_behavior(clear_blackboard=True)

    assert success is False
    assert manager._executor_settled_confirmed is False


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
