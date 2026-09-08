import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Time
import pytest
from procedure_spec import load_bundle, scenario_config_payload
from surgical_interop_msgs.action import ExecuteToolHandover
from surgical_interop_msgs.msg import BedRobotArmState, BedRobotArmStateArray
from surgical_interop_msgs.srv import ExecuteRetractionCommand

from surgical_interop_execution import bridge as bridge_module
from surgical_interop_execution.bridge import (
    ActiveAction,
    ActiveService,
    SurgicalInteropExecutionBridge,
    _bundle_config_revision,
    procedure_retraction_distance_limit_mm,
)
from surgical_interop_execution.direct_hand_ledger import DurableDirectHandLedger
from surgical_interop_execution.mappings import (
    DispatchLedger,
    InternalGroupCommand,
    InternalSkillCommand,
    MappingFailure,
    OPERATION_RETRACTION,
    RETRACTION_COMMAND_ADJUST_RETRACTION,
    RETRACTION_COMMAND_CHANGE_TOOL,
    RETRACTION_TARGET_LEFT,
    RETRACTION_TARGET_NONE,
    RETRACTION_TARGET_RIGHT,
    RetractionCommandRequest,
    ToolHandoverRequest,
    map_group_command,
)
from surgical_interop_execution.virtual_endpoints import (
    EXECUTION_ROUTE_STATE_SCHEMA,
    EXTERNAL_CONTROLLER_CONTRACT_TOPIC,
    EXTERNAL_RETRACTION_SERVICE_ENDPOINT,
    EXTERNAL_TOOL_HANDOVER_ENDPOINT,
    VIRTUAL_CONTROLLER_CONTRACT_TOPIC,
    VIRTUAL_RETRACTION_SERVICE_ENDPOINT,
    VIRTUAL_TOOL_HANDOVER_ENDPOINT,
    parse_execution_route_state,
    validate_virtual_endpoint_configuration,
)


def test_virtual_endpoint_source_requires_isolated_service_and_action_names() -> None:
    assert validate_virtual_endpoint_configuration(
        robot_endpoint_source="virtual",
        tool_handover_endpoint=VIRTUAL_TOOL_HANDOVER_ENDPOINT,
        retraction_service_name=VIRTUAL_RETRACTION_SERVICE_ENDPOINT,
        require_bed_robot_status=False,
    ) == "virtual"

    with pytest.raises(ValueError, match="isolated tool handover"):
        validate_virtual_endpoint_configuration(
            robot_endpoint_source="virtual",
            tool_handover_endpoint="/surgery/tool_handover",
            retraction_service_name=VIRTUAL_RETRACTION_SERVICE_ENDPOINT,
            require_bed_robot_status=False,
        )
    with pytest.raises(ValueError, match="isolated retraction service"):
        validate_virtual_endpoint_configuration(
            robot_endpoint_source="virtual",
            tool_handover_endpoint=VIRTUAL_TOOL_HANDOVER_ENDPOINT,
            retraction_service_name="/surgery/retraction/command",
            require_bed_robot_status=False,
        )
    with pytest.raises(ValueError, match="Service-only"):
        validate_virtual_endpoint_configuration(
            robot_endpoint_source="virtual",
            tool_handover_endpoint=VIRTUAL_TOOL_HANDOVER_ENDPOINT,
            retraction_service_name=VIRTUAL_RETRACTION_SERVICE_ENDPOINT,
            require_bed_robot_status=True,
        )
    with pytest.raises(ValueError, match="external endpoint mode"):
        validate_virtual_endpoint_configuration(
            robot_endpoint_source="external",
            tool_handover_endpoint=VIRTUAL_TOOL_HANDOVER_ENDPOINT,
            retraction_service_name="/surgery/retraction/command",
            require_bed_robot_status=False,
        )


def test_execution_route_state_allows_independently_validated_action_and_service_sources() -> None:
    virtual_payload = {
        "schema": EXECUTION_ROUTE_STATE_SCHEMA,
        "revision": 3,
        "initialization_revision": 8,
        "selected_source": "virtual",
        "run_endpoint_source": "",
        "retraction_source": "virtual",
        "run_retraction_source": "",
        "initialization_state": "initialized",
        "tool_handover_endpoint": VIRTUAL_TOOL_HANDOVER_ENDPOINT,
        "retraction_service_name": VIRTUAL_RETRACTION_SERVICE_ENDPOINT,
        "controller_contract_topic": VIRTUAL_CONTROLLER_CONTRACT_TOPIC,
        "expected_controller_contract_id": "taskplanner-virtual-eir-nuc.v1",
        "expected_capability_policy_id": "taskplanner-virtual-full-inventory.v1",
        "retraction_controller_contract_topic": VIRTUAL_CONTROLLER_CONTRACT_TOPIC,
        "retraction_expected_controller_contract_id": "taskplanner-virtual-eir-nuc.v1",
        "require_bed_robot_status": False,
        "require_physical_stop_confirmation": False,
        "retraction_state_machine_suppressed": True,
    }

    state = parse_execution_route_state(virtual_payload)
    assert state.selected_source == "virtual"
    assert state.tool_handover_endpoint == VIRTUAL_TOOL_HANDOVER_ENDPOINT
    assert state.retraction_service_name == VIRTUAL_RETRACTION_SERVICE_ENDPOINT

    mixed_payload = dict(virtual_payload)
    mixed_payload.update(
        {
            "selected_source": "external",
            "tool_handover_endpoint": EXTERNAL_TOOL_HANDOVER_ENDPOINT,
            "controller_contract_topic": EXTERNAL_CONTROLLER_CONTRACT_TOPIC,
            "expected_controller_contract_id": "eir-nuc-tool-handover.real.v1",
            "expected_capability_policy_id": "eir-nuc-tool-handover.v1",
        }
    )
    mixed = parse_execution_route_state(mixed_payload)
    assert mixed.selected_source == "external"
    assert mixed.retraction_source == "virtual"
    assert mixed.retraction_service_name == VIRTUAL_RETRACTION_SERVICE_ENDPOINT

    external_payload = dict(virtual_payload)
    external_payload.update(
        {
            "selected_source": "external",
            "retraction_source": "external",
            "tool_handover_endpoint": EXTERNAL_TOOL_HANDOVER_ENDPOINT,
            "retraction_service_name": EXTERNAL_RETRACTION_SERVICE_ENDPOINT,
            "controller_contract_topic": EXTERNAL_CONTROLLER_CONTRACT_TOPIC,
            "expected_controller_contract_id": "eir-nuc-tool-handover.real.v1",
            "expected_capability_policy_id": "eir-nuc-tool-handover.v1",
            "retraction_controller_contract_topic": EXTERNAL_CONTROLLER_CONTRACT_TOPIC,
            "retraction_expected_controller_contract_id": "eir-nuc-tool-handover.real.v1",
            "retraction_state_machine_suppressed": False,
        }
    )
    assert parse_execution_route_state(external_payload).selected_source == "external"


def test_execution_route_contract_metadata_is_optional() -> None:
    payload = {
        "schema": EXECUTION_ROUTE_STATE_SCHEMA,
        "revision": 4,
        "initialization_revision": 9,
        "selected_source": "virtual",
        "run_endpoint_source": "",
        "retraction_source": "virtual",
        "run_retraction_source": "",
        "initialization_state": "initialized",
        "tool_handover_endpoint": VIRTUAL_TOOL_HANDOVER_ENDPOINT,
        "retraction_service_name": VIRTUAL_RETRACTION_SERVICE_ENDPOINT,
        "require_bed_robot_status": False,
        "require_physical_stop_confirmation": False,
        "retraction_state_machine_suppressed": True,
    }

    state = parse_execution_route_state(payload)

    assert state.controller_contract_topic == ""
    assert state.expected_controller_contract_id == ""
    assert state.expected_capability_policy_id == ""
    assert state.retraction_controller_contract_topic == ""
    assert state.retraction_expected_controller_contract_id == ""


def test_execution_route_state_ignores_malformed_contract_diagnostics() -> None:
    payload = {
        "schema": EXECUTION_ROUTE_STATE_SCHEMA,
        "revision": 5,
        "initialization_revision": 10,
        "selected_source": "virtual",
        "run_endpoint_source": "",
        "retraction_source": "virtual",
        "run_retraction_source": "",
        "initialization_state": "initialized",
        "tool_handover_endpoint": VIRTUAL_TOOL_HANDOVER_ENDPOINT,
        "retraction_service_name": VIRTUAL_RETRACTION_SERVICE_ENDPOINT,
        # A wrong-family topic is retained for diagnostics only. Non-text and
        # oversized metadata are discarded instead of rejecting the route.
        "controller_contract_topic": EXTERNAL_CONTROLLER_CONTRACT_TOPIC,
        "expected_controller_contract_id": {"unexpected": "object"},
        "expected_capability_policy_id": "x" * 193,
        "retraction_controller_contract_topic": 17,
        "retraction_expected_controller_contract_id": [],
        "require_bed_robot_status": False,
        "require_physical_stop_confirmation": False,
        "retraction_state_machine_suppressed": True,
    }

    state = parse_execution_route_state(payload)

    assert state.controller_contract_topic == EXTERNAL_CONTROLLER_CONTRACT_TOPIC
    assert state.expected_controller_contract_id == ""
    assert state.expected_capability_policy_id == ""
    assert state.retraction_controller_contract_topic == (
        EXTERNAL_CONTROLLER_CONTRACT_TOPIC
    )
    assert state.retraction_expected_controller_contract_id == ""


def _skill(command_id: str = "skill-1") -> InternalSkillCommand:
    return InternalSkillCommand(
        command_id=command_id,
        action="tool_handover",
        instrument_id="T04",
        instrument_instance_id="T04#1",
        source_location_type="tray_slot",
        source_location_id="tray-a-2",
        target_location_type="handover_zone",
        target_location_id="surgeon_receive_zone",
        arm="right",
        request_generation=4,
        procedure_run_id="a" * 32,
        mode="explicit_request",
        rationale="internal only",
        target_owner="surgeon",
        cleaning_required=True,
    )


def _group(command_id: str = "group-1") -> InternalGroupCommand:
    return InternalGroupCommand(
        request_id="request-1",
        command_id=command_id,
        group_id="retraction",
        operation="retraction",
        arm_id="",
        target_tool_id="",
        adjustment_mode="single",
        target_retractor_id="left_malleable",
        direction_frame="surgeon_view",
        direction="left",
        axis="none",
        distance_mm=5.0,
        end_effector_profile="left_malleable",
    )


def test_loaded_demo_distance_policy_tightens_bridge_boundary() -> None:
    spec_dir = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    procedure_spec = load_bundle(spec_dir)
    limit_mm = procedure_retraction_distance_limit_mm(
        procedure_spec,
        configured_limit_mm=50.0,
    )

    assert limit_mm == 30.0
    with pytest.raises(MappingFailure, match="invalid_retraction_distance"):
        map_group_command(
            replace(
                _group("over-demo-limit"),
                operation=OPERATION_RETRACTION,
                distance_mm=30.1,
            ),
            max_retraction_distance_mm=limit_mm,
        )


def test_bridge_workflow_selection_comes_from_authored_runtime_requirements() -> None:
    spec_dir = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "nephrectomy"
    )
    procedure_spec = load_bundle(spec_dir)
    runtime = replace(
        procedure_spec.get_scenario_runtime_requirements(),
        retraction_workflow_state_enforced=False,
    )
    authored_policy = replace(
        procedure_spec.bundle.scenario_policy,
        runtime_requirements=runtime,
    )
    procedure_spec = procedure_spec.__class__(
        replace(procedure_spec.bundle, scenario_policy=authored_policy)
    )
    bridge = SurgicalInteropExecutionBridge.__new__(
        SurgicalInteropExecutionBridge
    )
    bridge._procedure_spec = procedure_spec

    assert procedure_spec.procedure_id == "nephrectomy"
    assert bridge._retraction_workflow_state_enforced() is False


def _tool_change_group(command_id: str = "tool-change-1") -> InternalGroupCommand:
    return replace(
        _group(command_id),
        operation="change_end_effector",
        arm_id="arm_1",
        target_tool_id="army_navy_retractor",
        adjustment_mode="",
        target_retractor_id="",
        direction_frame="",
        direction="",
        axis="",
        distance_mm=0.0,
        end_effector_profile="army_navy_retractor",
    )


def _retraction_request(
    command_id: str = "adjust-1",
    *,
    target_side: int = RETRACTION_TARGET_LEFT,
    command: int = RETRACTION_COMMAND_ADJUST_RETRACTION,
    distance_m: float = 0.005,
) -> RetractionCommandRequest:
    return RetractionCommandRequest(
        command_id=command_id,
        command=command,
        target_side=target_side,
        distance_m=distance_m,
    )


class _GoalHandle:
    def __init__(self):
        self.cancel_calls = 0
        self.accepted = True
        self.result_future = SimpleNamespace(add_done_callback=lambda callback: None)

    def cancel_goal_async(self):
        self.cancel_calls += 1

    def get_result_async(self):
        return self.result_future


def _bare_bridge() -> SurgicalInteropExecutionBridge:
    bridge = SurgicalInteropExecutionBridge.__new__(SurgicalInteropExecutionBridge)
    bridge._dispatch_lock = threading.RLock()
    bridge._runtime_accepting_commands = True
    bridge._dispatch_epoch = 0
    bridge._retraction_source_id = "taskplanner-test"
    bridge._dispatch_ledger = DispatchLedger(max_entries=8)
    bridge._active_actions = {}
    bridge._active_services = {}
    bridge._queued_voice_tool_transfer = None
    bridge._startup_actors_pending = False
    bridge._deferred_startup_tool_transfer = None
    bridge._tool_handover_enabled = True
    bridge._require_bed_robot_status = True
    bridge._bed_robot_status_timeout_sec = 2.0
    # Most unit fixtures use small synthetic timestamps to test ordering. Tests
    # that exercise absolute freshness override this with the production limit.
    bridge._bed_robot_source_max_age_sec = 10_000_000_000.0
    bridge._bed_robot_source_future_tolerance_sec = 0.5
    bridge._bed_robot_revision = None
    bridge._bed_robot_source_stamp_ns = None
    bridge._bed_robot_epoch = 0
    bridge._bed_robot_signature = None
    bridge._bed_robot_procedure_type = ""
    bridge._bed_robot_received_monotonic = 0.0
    bridge._bed_robot_states = {}
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=lambda _event: None)
    bridge._execution_trace_run_by_command = {}
    bridge._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="running",
        procedure_run_id="a" * 32,
    )
    bridge._latest_simulation_state_received_monotonic = time.monotonic()
    return bridge


def _route_coordinator_bridge() -> SurgicalInteropExecutionBridge:
    """Small no-ROS fixture for the stopped-only route coordinator."""

    bridge = _bare_bridge()
    bridge._robot_endpoint_source = "external"
    bridge._tool_transfer_endpoint = EXTERNAL_TOOL_HANDOVER_ENDPOINT
    bridge._retraction_service_name = EXTERNAL_RETRACTION_SERVICE_ENDPOINT
    bridge._retraction_endpoint_source = "external"
    bridge._route_selection_state_path = ""
    bridge._route_selection_runtime_mode = ""
    bridge._route_revision = 4
    bridge._route_initialization_revision = 9
    bridge._route_initialization_state = "stopped"
    bridge._run_endpoint_source = ""
    return bridge


def _scenario_observer_bridge() -> SurgicalInteropExecutionBridge:
    """Build the execution owner without ROS for ScenarioStore observer tests."""

    bridge = _bare_bridge()
    spec_root = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    ).resolve()
    bootstrap_dir = spec_root / "thyroidectomy_demo"
    bootstrap_spec = load_bundle(bootstrap_dir)
    bridge._spec_dir = str(bootstrap_dir)
    bridge._scenario_config_root = spec_root
    bridge._scenario_config_revision = ""
    bridge._pending_scenario_config = None
    bridge._procedure_spec = bootstrap_spec
    bridge._instrument_names = {
        instrument.id: instrument.display_name.strip()
        for instrument in bootstrap_spec.bundle.instruments
    }
    bridge._configured_max_retraction_distance_mm = 50.0
    bridge._max_retraction_distance_mm = procedure_retraction_distance_limit_mm(
        bootstrap_spec,
        configured_limit_mm=bridge._configured_max_retraction_distance_mm,
    )
    bridge._last_lifecycle_control_signature = None
    bridge._latest_simulation_state = SimpleNamespace(
        running=False,
        execution_state="idle",
    )
    bridge._latest_simulation_state_received_monotonic = time.monotonic()
    bridge.get_logger = lambda: SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    )
    return bridge


def _scenario_config_message(spec_dir: Path, *, revision: str | None = None):
    resolved = spec_dir.resolve()
    return SimpleNamespace(
        data=json.dumps(
            scenario_config_payload(
                bundle_name=resolved.name,
                spec_dir=str(resolved),
                revision=revision or _bundle_config_revision(resolved),
            )
        )
    )


def test_execution_bridge_observes_latched_scenario_store_revision_when_stopped(
) -> None:
    bridge = _scenario_observer_bridge()
    candidate = bridge._scenario_config_root / "nephrectomy"

    bridge._on_scenario_config(_scenario_config_message(candidate))

    assert bridge._procedure_spec.procedure_id == "nephrectomy"
    assert bridge._spec_dir == str(candidate.resolve())
    assert bridge._scenario_config_revision == _bundle_config_revision(candidate)
    assert bridge._pending_scenario_config is None


def test_execution_bridge_observes_latched_scenario_store_revision_when_paused(
) -> None:
    bridge = _scenario_observer_bridge()
    bridge._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="paused",
    )
    candidate = bridge._scenario_config_root / "nephrectomy"

    bridge._on_scenario_config(_scenario_config_message(candidate))

    assert bridge._procedure_spec.procedure_id == "nephrectomy"
    assert bridge._spec_dir == str(candidate.resolve())
    assert bridge._scenario_config_revision == _bundle_config_revision(candidate)
    assert bridge._pending_scenario_config is None


@pytest.mark.parametrize(
    "blocker",
    ["running_scenario", "active_controller_request"],
)
def test_execution_bridge_defers_scenario_store_revision_until_its_own_boundary(
    blocker: str,
) -> None:
    bridge = _scenario_observer_bridge()
    candidate = bridge._scenario_config_root / "nephrectomy"
    if blocker == "running_scenario":
        bridge._latest_simulation_state = SimpleNamespace(
            running=True,
            execution_state="running",
        )
    else:
        bridge._active_actions[("tool_transfer", "in-flight")] = ActiveAction(
            route="tool_transfer",
            command=_skill("in-flight"),
            dispatched=True,
        )

    bridge._on_scenario_config(_scenario_config_message(candidate))

    assert bridge._procedure_spec.procedure_id == "thyroidectomy_demo"
    assert bridge._pending_scenario_config is not None

    bridge._active_actions.clear()
    bridge._on_simulation_state(
        SimpleNamespace(running=False, execution_state="halted")
    )

    assert bridge._procedure_spec.procedure_id == "nephrectomy"
    assert bridge._pending_scenario_config is None


@pytest.mark.parametrize(
    "blocker",
    ["active_action", "active_service", "execution_proxy"],
)
def test_execution_bridge_paused_scenario_config_waits_for_local_work_to_finish(
    blocker: str,
) -> None:
    bridge = _scenario_observer_bridge()
    bridge._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="paused",
    )
    candidate = bridge._scenario_config_root / "nephrectomy"
    if blocker == "active_action":
        bridge._active_actions[("tool_transfer", "in-flight")] = ActiveAction(
            route="tool_transfer",
            command=_skill("in-flight"),
            dispatched=True,
        )
    elif blocker == "active_service":
        bridge._active_services[("retraction", "in-flight")] = ActiveService(
            route="retraction",
            command=_group("in-flight"),
            dispatched=True,
        )
    else:
        bridge._execution_proxy_active = True

    bridge._on_scenario_config(_scenario_config_message(candidate))

    assert bridge._procedure_spec.procedure_id == "thyroidectomy_demo"
    assert bridge._pending_scenario_config is not None

    bridge._active_actions.clear()
    bridge._active_services.clear()
    bridge._execution_proxy_active = False
    bridge._on_simulation_state(
        SimpleNamespace(running=True, execution_state="paused")
    )

    assert bridge._procedure_spec.procedure_id == "nephrectomy"
    assert bridge._pending_scenario_config is None


def test_execution_bridge_discards_bad_scenario_revision_and_keeps_last_good() -> None:
    bridge = _scenario_observer_bridge()
    candidate = bridge._scenario_config_root / "nephrectomy"

    bridge._on_scenario_config(
        _scenario_config_message(candidate, revision="sha256:" + "0" * 64)
    )

    assert bridge._procedure_spec.procedure_id == "thyroidectomy_demo"
    assert bridge._scenario_config_revision == ""
    assert bridge._pending_scenario_config is None


def test_execution_bridge_rejects_scenario_path_outside_fixed_root() -> None:
    bridge = _scenario_observer_bridge()
    outside = bridge._scenario_config_root.parent
    message = SimpleNamespace(
        data=json.dumps(
            scenario_config_payload(
                bundle_name=outside.name,
                spec_dir=str(outside),
                revision="sha256:" + "0" * 64,
            )
        )
    )

    bridge._on_scenario_config(message)

    assert bridge._procedure_spec.procedure_id == "thyroidectomy_demo"
    assert bridge._pending_scenario_config is None


def test_execution_bridge_rejects_direct_spec_parameter_selection() -> None:
    bridge = _scenario_observer_bridge()
    result = bridge._on_endpoint_configuration_parameters_changed(
        [SimpleNamespace(name="spec_dir", value="/untrusted/bundle")]
    )

    assert result.successful is False
    assert "ScenarioStore owns scenario selection" in result.reason


def _direct_hand_skill(run_id: str = "a" * 32, generation: int = 1):
    return replace(
        _skill("skill-hand-test"),
        action="direct_handover",
        mode="implicit_request",
        request_generation=0,
        procedure_run_id=run_id,
        implicit_request_generation=generation,
        voice_backed=False,
    )


def test_direct_hand_command_is_bound_to_fresh_running_state() -> None:
    bridge = _bare_bridge()
    bridge._direct_hand_dispatch_ledger = DurableDirectHandLedger(":memory:")
    bridge._direct_hand_state_max_age_sec = 1.0
    bridge._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="running",
        procedure_run_id="a" * 32,
    )
    bridge._latest_simulation_state_received_monotonic = time.monotonic()

    assert bridge._direct_hand_run_guard(_direct_hand_skill()) == ""

    assert bridge._direct_hand_run_guard(
        _direct_hand_skill("b" * 32)
    ) == "direct_hand_run_mismatch"
    bridge._latest_simulation_state.running = False
    assert bridge._direct_hand_run_guard(
        _direct_hand_skill()
    ) == "direct_hand_runtime_not_running"


def test_only_completion_cleanup_returns_are_admitted_while_finishing() -> None:
    bridge = _bare_bridge()
    bridge._direct_hand_state_max_age_sec = 1.0
    bridge._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="finishing",
        procedure_run_id="a" * 32,
    )
    bridge._latest_simulation_state_received_monotonic = time.monotonic()

    cleanup = replace(
        _skill("finish-return-to-tray"),
        action="return_preposition_to_tray",
        source_location_type="robot_right_hand",
        source_location_id="robot_right_hand",
        target_location_type="tray_slot",
        target_location_id="main_tray_slot_4",
        mode="recovery",
    )
    assert bridge._direct_hand_run_guard(cleanup) == ""

    retrieval = replace(
        cleanup,
        command_id="finish-retrieve-from-mayo",
        action="retrieve_from_mayo",
        source_location_type="mayo_stand",
        source_location_id="mayo_stand",
        target_location_type="tray_slot",
    )
    assert bridge._direct_hand_run_guard(retrieval) == ""

    ordinary_prepare = replace(cleanup, action="prepare_tool")
    assert (
        bridge._direct_hand_run_guard(ordinary_prepare)
        == "command_runtime_not_running"
    )


def test_direct_hand_command_fails_closed_without_identity_or_durable_ledger() -> None:
    bridge = _bare_bridge()
    bridge._direct_hand_dispatch_ledger = DurableDirectHandLedger(":memory:")
    bridge._direct_hand_state_max_age_sec = 1.0
    bridge._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="running",
        procedure_run_id="a" * 32,
    )
    bridge._latest_simulation_state_received_monotonic = time.monotonic()

    assert bridge._direct_hand_run_guard(
        _direct_hand_skill(run_id="", generation=0)
    ) == "direct_hand_episode_invalid"

    bridge._direct_hand_dispatch_ledger = None
    assert bridge._direct_hand_run_guard(
        _direct_hand_skill()
    ) == "direct_hand_ledger_unavailable"


def test_public_route_coordinator_uses_only_atomic_stopped_boundary() -> None:
    bridge = _route_coordinator_bridge()
    events: list[object] = []

    def snapshot() -> dict[str, object]:
        return {
            "selected_source": bridge._robot_endpoint_source,
            "retraction_source": bridge._retraction_endpoint_source,
            "revision": bridge._route_revision,
            "initialization_revision": bridge._route_initialization_revision,
            "initialization_state": bridge._route_initialization_state,
        }

    bridge._execution_route_switch_guard_for_target_locked = lambda **_kwargs: ""

    def set_route(
        source: str,
        *,
        retraction_source: str,
        clear_admission: bool,
    ) -> None:
        events.append(("swap", source, clear_admission))
        bridge._robot_endpoint_source = source
        bridge._retraction_endpoint_source = retraction_source
        bridge._tool_transfer_endpoint = VIRTUAL_TOOL_HANDOVER_ENDPOINT
        bridge._retraction_service_name = VIRTUAL_RETRACTION_SERVICE_ENDPOINT

    bridge._set_route_source_locked = set_route
    bridge._execution_route_state_snapshot_locked = snapshot
    bridge._execution_route_state_snapshot = snapshot
    bridge._publish_execution_route_state = lambda: events.append(
        ("publish", bridge._route_initialization_state)
    )

    response = bridge._handle_execution_route_command(
        SimpleNamespace(
            operation="configure_robot_endpoint_source",
            payload_json='{"source":"virtual"}',
        ),
        SimpleNamespace(),
    )

    assert response.accepted is True
    assert events == [
        ("swap", "virtual", True),
        ("publish", "initialized"),
    ]
    result = json.loads(response.result_json)
    assert "digital_twin_reset" not in result
    assert result["selected_source"] == "virtual"
    assert result["initialization_state"] == "initialized"

    # The dependent methods are not merely skipped in one branch: the bridge
    # no longer owns either a manager-reset or preflight-ack dependency.
    assert not hasattr(
        SurgicalInteropExecutionBridge,
        "_manager_transition_ready_for_execution_route",
    )
    assert not hasattr(
        SurgicalInteropExecutionBridge, "_request_execution_route_reset"
    )
    assert not hasattr(
        SurgicalInteropExecutionBridge, "_wait_for_preflight_route_ack"
    )


def test_public_route_coordinator_rejects_active_request_without_external_waits() -> None:
    bridge = _route_coordinator_bridge()
    bridge._execution_route_switch_guard_for_target_locked = (
        lambda **_kwargs: "active_controller_request"
    )
    bridge._execution_route_state_snapshot = lambda: {
        "selected_source": "external",
        "revision": 4,
    }

    response = bridge._handle_execution_route_command(
        SimpleNamespace(
            operation="configure_robot_endpoint_source",
            payload_json='{"source":"virtual"}',
        ),
        SimpleNamespace(),
    )

    assert response.accepted is False
    assert response.message == "active_controller_request"
    assert "digital_twin_reset" not in json.loads(response.result_json)


@pytest.mark.parametrize("execution_state", ["idle", "halted", "completed", "terminated"])
def test_route_switch_guard_accepts_manager_terminal_stopped_states(
    execution_state: str,
) -> None:
    bridge = _bare_bridge()
    bridge._runtime_accepting_commands = False
    bridge._latest_simulation_state = SimpleNamespace(
        running=False,
        execution_state=execution_state,
        active_robot_task_id="",
        cleaner_busy=False,
    )
    bridge._latest_simulation_state_received_monotonic = time.monotonic()

    assert bridge._execution_route_switch_guard_locked() == ""


def test_paused_scenario_config_guard_does_not_relax_route_switch_boundary() -> None:
    bridge = _bare_bridge()
    bridge._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="paused",
    )

    assert bridge._scenario_config_switch_guard_locked() == ""
    assert bridge._execution_route_switch_guard_locked() == "simulation_not_stopped"


def test_route_switch_guard_keeps_active_request_blocked_after_completion() -> None:
    bridge = _bare_bridge()
    bridge._runtime_accepting_commands = False
    bridge._latest_simulation_state = SimpleNamespace(
        running=False,
        execution_state="completed",
        active_robot_task_id="",
        cleaner_busy=False,
    )
    bridge._latest_simulation_state_received_monotonic = time.monotonic()
    bridge._active_services[("retraction", "still-active")] = ActiveService(
        route="retraction",
        command=_group("still-active"),
        dispatched=True,
    )

    assert bridge._execution_route_switch_guard_locked() == "active_controller_request"


def test_target_route_guard_allows_only_a_stopped_external_orphan_to_virtual() -> None:
    """A dead external endpoint may be escaped without calling it complete."""

    bridge = _route_coordinator_bridge()
    bridge._latest_simulation_state = SimpleNamespace(
        running=False,
        execution_state="idle",
    )
    unavailable_action = SimpleNamespace(server_is_ready=lambda: False)
    unavailable_service = SimpleNamespace(service_is_ready=lambda: False)
    bridge._external_tool_transfer_client = unavailable_action
    bridge._external_retraction_service_client = unavailable_service
    bridge._virtual_tool_transfer_client = SimpleNamespace(server_is_ready=lambda: True)
    bridge._virtual_retraction_service_client = SimpleNamespace(
        service_is_ready=lambda: True
    )
    bridge._active_actions[("tool_transfer", "stale-external")] = ActiveAction(
        route="tool_transfer",
        command=_skill("stale-external"),
        endpoint_source="external",
        cancelled=True,
        dispatched=True,
    )

    assert bridge._execution_route_switch_guard_for_target_locked(
        requested_source="virtual",
        requested_retraction_source="external",
    ) == ""
    # The recovery record remains active for owner restart and external re-entry.
    assert bridge._execution_route_switch_guard_locked() == "active_controller_request"
    assert bridge._execution_route_switch_guard_for_target_locked(
        requested_source="external",
        requested_retraction_source="external",
    ) == "active_controller_request"


def test_target_route_guard_never_bypasses_a_live_or_virtual_request() -> None:
    bridge = _route_coordinator_bridge()
    bridge._latest_simulation_state = SimpleNamespace(
        running=False,
        execution_state="idle",
    )
    bridge._external_tool_transfer_client = SimpleNamespace(server_is_ready=lambda: False)
    bridge._virtual_tool_transfer_client = SimpleNamespace(server_is_ready=lambda: False)
    bridge._external_retraction_service_client = SimpleNamespace(
        service_is_ready=lambda: False
    )
    bridge._virtual_retraction_service_client = SimpleNamespace(
        service_is_ready=lambda: False
    )
    bridge._active_actions[("tool_transfer", "live-external")] = ActiveAction(
        route="tool_transfer",
        command=_skill("live-external"),
        endpoint_source="external",
        cancelled=False,
        dispatched=True,
    )

    assert bridge._execution_route_switch_guard_for_target_locked(
        requested_source="virtual",
        requested_retraction_source="virtual",
    ) == "active_controller_request"


def test_route_switch_preserves_an_escaped_legacy_external_recovery_record() -> None:
    bridge = _route_coordinator_bridge()
    bridge._latest_simulation_state = SimpleNamespace(
        running=False,
        execution_state="idle",
    )
    bridge._external_tool_transfer_client = SimpleNamespace(server_is_ready=lambda: False)
    bridge._external_retraction_service_client = SimpleNamespace(
        service_is_ready=lambda: False
    )
    bridge._virtual_tool_transfer_client = SimpleNamespace(server_is_ready=lambda: True)
    bridge._virtual_retraction_service_client = SimpleNamespace(
        service_is_ready=lambda: True
    )
    stale = ActiveAction(
        route="tool_transfer",
        command=_skill("legacy-stale-external"),
        cancelled=True,
        dispatched=True,
    )
    bridge._active_actions[("tool_transfer", stale.command.command_id)] = stale
    bridge._execution_route_state_snapshot_locked = lambda: {
        "selected_source": bridge._robot_endpoint_source,
        "retraction_source": bridge._retraction_endpoint_source,
        "revision": bridge._route_revision,
    }
    bridge._publish_execution_route_state = lambda: None

    def set_route(source: str, *, retraction_source: str, clear_admission: bool) -> None:
        del clear_admission
        bridge._robot_endpoint_source = source
        bridge._retraction_endpoint_source = retraction_source

    bridge._set_route_source_locked = set_route
    response = bridge._handle_execution_route_switch(
        requested_source="virtual",
        requested_retraction_source="external",
        response=SimpleNamespace(),
    )

    assert response.accepted is True
    assert bridge._robot_endpoint_source == "virtual"
    # The record was not discarded or treated as completed; it stays bound to
    # the old endpoint and continues to block a later external route/restart.
    assert stale.endpoint_source == "external"
    assert bridge._execution_route_switch_guard_locked() == "active_controller_request"

    bridge._active_actions.clear()
    bridge._active_actions[("tool_transfer", "stale-virtual")] = ActiveAction(
        route="tool_transfer",
        command=_skill("stale-virtual"),
        endpoint_source="virtual",
        cancelled=True,
        dispatched=True,
    )
    assert bridge._execution_route_switch_guard_for_target_locked(
        requested_source="virtual",
        requested_retraction_source="virtual",
    ) == "active_controller_request"


def test_route_state_projects_owner_restart_boundary_without_endpoint_gating() -> None:
    """A stopped execution owner may restart even if an endpoint is offline."""

    bridge = _route_coordinator_bridge()
    bridge._retraction_endpoint_source = "external"
    bridge._run_retraction_source = ""
    bridge._controller_contract_topic = EXTERNAL_CONTROLLER_CONTRACT_TOPIC
    bridge._expected_controller_contract_id = "eir-nuc-tool-handover.real.v1"
    bridge._expected_capability_policy_id = "eir-nuc-tool-handover.v1"
    bridge._retraction_controller_contract_topic = EXTERNAL_CONTROLLER_CONTRACT_TOPIC
    bridge._retraction_expected_controller_contract_id = (
        "eir-nuc-tool-handover.real.v1"
    )
    bridge._require_physical_stop_confirmation = True
    bridge._virtual_endpoint_mode = False
    bridge._retraction_workflow_state_enforced = lambda: True
    unavailable_action = SimpleNamespace(server_is_ready=lambda: False)
    unavailable_service = SimpleNamespace(service_is_ready=lambda: False)
    bridge._tool_transfer_client = unavailable_action
    bridge._retraction_service_client = unavailable_service
    bridge._external_tool_transfer_client = unavailable_action
    bridge._external_retraction_service_client = unavailable_service
    bridge._virtual_tool_transfer_client = unavailable_action
    bridge._virtual_retraction_service_client = unavailable_service
    bridge._enable_runtime_route_control = True
    bridge._route_command_service = object()
    bridge._latest_simulation_state = SimpleNamespace(
        running=False,
        execution_state="idle",
    )

    state = bridge._execution_route_state_snapshot()

    assert state["action_server_ready"] is False
    assert state["retraction_service_ready"] is False
    assert state["restart_allowed"] is True
    assert state["restart_blocker"] == ""

    bridge._active_actions[("tool_transfer", "restart-blocker")] = ActiveAction(
        route="tool_transfer",
        command=_skill("restart-blocker"),
        dispatched=True,
    )
    state = bridge._execution_route_state_snapshot()
    assert state["restart_allowed"] is False
    assert state["restart_blocker"] == "active_controller_request"

    bridge._active_actions.clear()
    bridge._active_services[("retraction", "restart-blocker")] = ActiveService(
        route="retraction",
        command=_group("restart-blocker"),
        dispatched=True,
    )
    state = bridge._execution_route_state_snapshot()
    assert state["restart_allowed"] is False
    assert state["restart_blocker"] == "active_controller_request"

    bridge._active_services.clear()
    bridge._latest_simulation_state.running = True
    state = bridge._execution_route_state_snapshot()
    assert state["restart_allowed"] is False
    assert state["restart_blocker"] == "simulation_not_stopped"


def test_integration_readiness_is_observation_only_telemetry() -> None:
    bridge = _bare_bridge()
    unreadied = {
        "schema": "taskplanner.integration_readiness.v1",
        "ready": False,
        "checks": {"tool_handover_action_server": False},
    }

    bridge._on_integration_readiness(
        SimpleNamespace(data=json.dumps(unreadied))
    )

    assert bridge._latest_integration_readiness == unreadied
    assert bridge._latest_integration_readiness_error == ""
    assert bridge._latest_integration_readiness_received_monotonic > 0.0
    bridge._on_integration_readiness(SimpleNamespace(data="not-json"))
    assert bridge._latest_integration_readiness == unreadied
    assert bridge._latest_integration_readiness_error == (
        "integration_readiness_invalid_json"
    )
    assert not hasattr(SurgicalInteropExecutionBridge, "_dispatch_admission_guard")
    assert not hasattr(
        SurgicalInteropExecutionBridge, "_integration_readiness_lease_guard"
    )


@pytest.mark.parametrize(
    ("endpoint_source", "tool_handover_endpoint"),
    [
        ("external", EXTERNAL_TOOL_HANDOVER_ENDPOINT),
        ("virtual", VIRTUAL_TOOL_HANDOVER_ENDPOINT),
    ],
)
def test_tool_handover_reaches_send_goal_without_controller_contract(
    endpoint_source: str,
    tool_handover_endpoint: str,
) -> None:
    bridge = _bare_bridge()
    bridge._robot_endpoint_source = endpoint_source
    bridge._retraction_endpoint_source = "virtual"
    bridge._tool_transfer_endpoint = tool_handover_endpoint
    bridge._enable_runtime_route_control = True
    bridge._server_wait_timeout_sec = 0.1
    # An explicitly unready preflight observation must never suppress a typed
    # Action whose selected endpoint is available.
    bridge._on_integration_readiness(
        SimpleNamespace(
            data=json.dumps(
                {
                    "schema": "taskplanner.integration_readiness.v1",
                    "ready": False,
                    "checks": {"tool_handover_action_server": False},
                }
            )
        )
    )
    submitted: list[tuple[object, object]] = []

    def dispatch_lock_is_available_to_another_thread() -> bool:
        acquired: list[bool] = []

        def probe() -> None:
            locked = bridge._dispatch_lock.acquire(timeout=0.2)
            acquired.append(locked)
            if locked:
                bridge._dispatch_lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=0.5)
        return acquired == [True]

    class ReadyActionClient:
        @staticmethod
        def wait_for_server(*, timeout_sec: float) -> bool:
            assert timeout_sec == 0.1
            assert dispatch_lock_is_available_to_another_thread()
            return True

        @staticmethod
        def send_goal_async(goal, *, feedback_callback):
            assert dispatch_lock_is_available_to_another_thread()
            submitted.append((goal, feedback_callback))
            return SimpleNamespace(add_done_callback=lambda _callback: None)

    bridge._tool_transfer_client = ReadyActionClient()
    statuses: list[dict[str, object]] = []
    traces: list[dict[str, object]] = []
    bridge._publish_skill_status = lambda _command, **kwargs: statuses.append(kwargs)
    bridge._publish_execution_trace = lambda **kwargs: traces.append(kwargs)
    command = _skill("no-controller-contract")
    request = ToolHandoverRequest(
        command_id=command.command_id,
        instrument_id="Bovie surgical cautery",
        instrument_instance_id="Bovie surgical cautery#1",
        source_location="tray",
        target_location="surgeon",
    )

    bridge._dispatch_tool_transfer(command, request)

    assert len(submitted) == 1
    assert statuses[0] == {
        "state": "dispatching",
        "success": True,
        "reason_code": "dispatching",
    }
    assert traces[-1]["stage"] == "sent"
    assert traces[-1]["reason_code"] == "goal_send_submitted"


def test_pause_invalidates_reserved_voice_dispatch_while_waiting_for_server() -> None:
    bridge = _bare_bridge()
    bridge._server_wait_timeout_sec = 0.1
    bridge._tool_transfer_endpoint = "/test/tool_handover"
    bridge._publish_execution_route_state = lambda: None
    statuses: list[tuple[str, dict[str, object]]] = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        (command.command_id, kwargs)
    )
    bridge._publish_execution_trace = lambda **_kwargs: None
    lock_available: list[bool] = []
    send_calls: list[object] = []

    class PauseDuringWaitClient:
        @staticmethod
        def wait_for_server(*, timeout_sec: float) -> bool:
            assert timeout_sec == 0.1
            acquired: list[bool] = []

            def probe() -> None:
                locked = bridge._dispatch_lock.acquire(timeout=0.2)
                acquired.append(locked)
                if locked:
                    bridge._dispatch_lock.release()

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=0.5)
            lock_available.extend(acquired)
            bridge._on_control(SimpleNamespace(data="pause"))
            return True

        @staticmethod
        def send_goal_async(goal, *, feedback_callback):
            send_calls.append((goal, feedback_callback))
            return SimpleNamespace(add_done_callback=lambda _callback: None)

    bridge._tool_transfer_client = PauseDuringWaitClient()
    command = _voice_skill("voice-paused-before-send", 91)
    request = _tool_transfer_request(command)
    assert (
        bridge._begin_action_dispatch(
            "tool_transfer",
            command,
            semantic_leg=("tray", "surgeon"),
        )
        == ""
    )
    tracked, _cancelled, _semantic_leg, expected_epoch = (
        bridge._tool_transfer_action_snapshot(command.command_id)
    )
    assert tracked

    bridge._dispatch_reserved_tool_transfer(
        command,
        request,
        expected_epoch=expected_epoch,
        server_ready=False,
    )

    assert lock_available == [True]
    assert send_calls == []
    assert not bridge._runtime_accepting_commands
    assert bridge._dispatch_epoch == expected_epoch + 1
    assert bridge._active_actions == {}
    assert statuses[-1][1]["state"] == ExecuteToolHandover.Result.FINAL_CANCELED
    assert statuses[-1][1]["reason_code"] == (
        ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED
    )


def test_tool_handover_lane_is_fail_closed_when_disabled_for_procedure() -> None:
    bridge = _bare_bridge()
    bridge._tool_handover_enabled = False
    bridge._skill_from_msg = lambda _message: _skill("disabled-handover")
    statuses = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        (command, kwargs)
    )
    bridge._dispatch_tool_transfer = lambda *_args: (_ for _ in ()).throw(
        AssertionError("disabled tool handover reached the Action client")
    )

    bridge._on_skill(SimpleNamespace())

    assert len(statuses) == 1
    assert statuses[0][1] == {
        "state": "rejected",
        "success": False,
        "reason_code": "tool_handover_disabled_for_procedure",
    }


def test_mayo_retrieval_is_rejected_while_right_hand_is_prepositioned() -> None:
    bridge = _bare_bridge()
    bridge._latest_simulation_state.right_hand_tool = "T04"
    bridge._latest_simulation_state.right_hand_tool_instance_id = "T04#1"
    command = replace(
        _skill("retrieve-with-right-preposition"),
        action="retrieve_from_mayo",
        arm="left",
        source_location_type="mayo_stand",
        source_location_id="mayo_stand",
        target_location_type="tray_slot",
        target_location_id="main_tray_slot_4",
    )
    bridge._skill_from_msg = lambda _message: command
    statuses = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        (command, kwargs)
    )
    bridge._dispatch_tool_transfer = lambda *_args: (_ for _ in ()).throw(
        AssertionError("right-hand preposition reached the Action client")
    )

    bridge._on_skill(SimpleNamespace())

    assert len(statuses) == 1
    assert statuses[0][1] == {
        "state": "rejected",
        "success": False,
        "reason_code": "retrieve_blocked_right_hand_preposition",
    }


def test_mayo_retrieval_guard_allows_an_empty_right_hand() -> None:
    bridge = _bare_bridge()
    command = replace(_skill("retrieve-with-empty-right"), action="retrieve_from_mayo")
    assert bridge._retrieval_run_guard(command) == ""


def _activate_tool_transfer(
    bridge: SurgicalInteropExecutionBridge,
    command: InternalSkillCommand,
    *,
    cancel_requested: bool = False,
    goal_handle=None,
    semantic_leg: tuple[str, str] | None = None,
) -> None:
    bridge._active_actions[("tool_transfer", command.command_id)] = ActiveAction(
        route="tool_transfer",
        command=command,
        goal_handle=goal_handle,
        cancelled=cancel_requested,
        semantic_leg=semantic_leg,
    )


@pytest.mark.parametrize(
    ("ros_status", "success", "final_state", "reason_code"),
    [
        (
            GoalStatus.STATUS_SUCCEEDED,
            True,
            ExecuteToolHandover.Result.FINAL_COMPLETED,
            ExecuteToolHandover.Result.REASON_COMPLETED,
        ),
        (
            GoalStatus.STATUS_CANCELED,
            False,
            ExecuteToolHandover.Result.FINAL_CANCELED,
            ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED,
        ),
        (
            GoalStatus.STATUS_ABORTED,
            False,
            ExecuteToolHandover.Result.FINAL_FAILED,
            "controller_failed",
        ),
    ],
)
def test_accepted_tool_action_projects_one_correlated_task_boundary_pair(
    ros_status: int,
    success: bool,
    final_state: str,
    reason_code: str,
) -> None:
    bridge = _bare_bridge()
    command = replace(
        _skill(f"task-boundary-{final_state}"),
        action="direct_handover",
        source_location_type="robot_right_hand",
        source_location_id="robot_right_hand",
        mode="implicit_request",
    )
    _activate_tool_transfer(
        bridge,
        command,
        semantic_leg=("robot", "surgeon"),
    )
    events = []
    bridge._stamp = lambda: Time(sec=17, nanosec=0)
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    bridge._publish_skill_status = lambda *_args, **_kwargs: None
    bridge._publish_execution_trace = lambda **_kwargs: None
    bridge._publish_tool_transfer_completed_events = lambda *_args, **_kwargs: None
    bridge._publish_tool_transfer_cancel_reconciliation = (
        lambda *_args, **_kwargs: None
    )
    goal_handle = _GoalHandle()
    accepted_future = SimpleNamespace(result=lambda: goal_handle)

    bridge._on_tool_transfer_goal_response(command, accepted_future)
    # A repeated callback must not manufacture a second active-task start.
    bridge._on_tool_transfer_goal_response(command, accepted_future)

    assert [event.event_type for event in events] == ["RobotTaskStarted"]
    started = events[0]
    assert started.instrument_id == command.instrument_id
    assert started.instance_id == command.instrument_instance_id
    assert started.arm == ""
    assert started.source_location_id == "robot_right_hand"
    assert started.target_location_id == "surgeon_receive_zone"
    started_detail = json.loads(started.detail_json)
    assert started_detail["task_id"] == command.command_id
    assert started_detail["task_type"] == "direct_handover"
    assert started_detail["transport"] == "ros2_action"

    terminal_future = SimpleNamespace(
        result=lambda: SimpleNamespace(
            status=ros_status,
            result=SimpleNamespace(
                success=success,
                final_state=final_state,
                reason_code=reason_code,
            ),
        )
    )
    bridge._on_tool_transfer_result(command, terminal_future)
    # The action is no longer tracked, so a duplicate terminal callback is a
    # no-op and cannot clear a later task with the same instrument.
    bridge._on_tool_transfer_result(command, terminal_future)

    assert [event.event_type for event in events] == [
        "RobotTaskStarted",
        "RobotTaskCompleted",
    ]
    completed_detail = json.loads(events[-1].detail_json)
    assert completed_detail["task_id"] == command.command_id
    assert completed_detail["controller_final_state"] == final_state
    assert completed_detail["controller_reason_code"] == reason_code


def test_rejected_or_unknown_goal_response_publishes_no_task_boundary() -> None:
    for command_id, future in (
        (
            "task-rejected-before-accept",
            SimpleNamespace(
                result=lambda: SimpleNamespace(accepted=False),
            ),
        ),
        (
            "task-goal-response-unknown",
            SimpleNamespace(
                result=lambda: (_ for _ in ()).throw(
                    RuntimeError("goal response lost")
                )
            ),
        ),
    ):
        bridge = _bare_bridge()
        command = replace(_skill(command_id), action="direct_handover")
        _activate_tool_transfer(bridge, command)
        events = []
        bridge._skill_event_pub = SimpleNamespace(publish=events.append)
        bridge._publish_skill_status = lambda *_args, **_kwargs: None
        bridge._publish_execution_trace = lambda **_kwargs: None

        bridge._on_tool_transfer_goal_response(command, future)

        assert events == []


def test_unknown_result_after_acceptance_keeps_task_active_without_false_completion() -> None:
    bridge = _bare_bridge()
    command = replace(
        _skill("task-result-unknown"),
        action="direct_handover",
    )
    _activate_tool_transfer(bridge, command)
    events = []
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    bridge._publish_skill_status = lambda *_args, **_kwargs: None
    bridge._publish_execution_trace = lambda **_kwargs: None

    bridge._on_tool_transfer_goal_response(
        command,
        SimpleNamespace(result=lambda: _GoalHandle()),
    )
    bridge._on_tool_transfer_result(
        command,
        SimpleNamespace(
            result=lambda: (_ for _ in ()).throw(
                RuntimeError("result response lost")
            )
        ),
    )

    assert [event.event_type for event in events] == ["RobotTaskStarted"]
    assert ("tool_transfer", command.command_id) in bridge._active_actions
    assert bridge._runtime_is_accepting() is False


@pytest.mark.parametrize(
    ("ros_status", "expected_event_types"),
    [
        (
            GoalStatus.STATUS_ABORTED,
            ["RobotTaskStarted", "RobotTaskCompleted"],
        ),
        (GoalStatus.STATUS_UNKNOWN, ["RobotTaskStarted"]),
    ],
)
def test_missing_result_payload_only_completes_correlated_terminal_status(
    ros_status: int,
    expected_event_types: list[str],
) -> None:
    bridge = _bare_bridge()
    command = replace(
        _skill(f"task-missing-result-{ros_status}"),
        action="direct_handover",
    )
    _activate_tool_transfer(bridge, command)
    events = []
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    bridge._publish_skill_status = lambda *_args, **_kwargs: None
    bridge._publish_execution_trace = lambda **_kwargs: None

    bridge._on_tool_transfer_goal_response(
        command,
        SimpleNamespace(result=lambda: _GoalHandle()),
    )
    bridge._on_tool_transfer_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(status=ros_status, result=None),
        ),
    )

    assert [event.event_type for event in events] == expected_event_types
    if expected_event_types[-1] == "RobotTaskCompleted":
        detail = json.loads(events[-1].detail_json)
        assert detail["controller_final_state"] == "failed"
        assert detail["controller_reason_code"] == "invalid_controller_result"
    assert bridge._runtime_is_accepting() is False


def test_public_tool_handover_state_vocabulary_is_fixed_and_minimal():
    assert {
        ExecuteToolHandover.Feedback.STATE_MOVING_TO_SOURCE,
        ExecuteToolHandover.Feedback.STATE_GRASPING,
        ExecuteToolHandover.Feedback.STATE_MOVING_TO_TARGET,
        ExecuteToolHandover.Feedback.STATE_WAITING_FOR_TAKEOVER,
        ExecuteToolHandover.Feedback.STATE_PLACING,
        ExecuteToolHandover.Feedback.STATE_HOLDING,
        ExecuteToolHandover.Feedback.STATE_STOPPING,
        ExecuteToolHandover.Feedback.STATE_RETREATING,
        ExecuteToolHandover.Feedback.STATE_RECOVERING_TO_TRAY,
    } == {
        "moving_to_source",
        "grasping",
        "moving_to_target",
        "waiting_for_takeover",
        "placing",
        "holding",
        "stopping",
        "retreating",
        "recovering_to_tray",
    }
    assert {
        ExecuteToolHandover.Result.FINAL_COMPLETED,
        ExecuteToolHandover.Result.FINAL_CANCELED,
        ExecuteToolHandover.Result.FINAL_FAILED,
    } == {"completed", "canceled", "failed"}
    assert {
        ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED,
        ExecuteToolHandover.Result.REASON_CANCELED_RECOVERED_TO_TRAY,
    } == {"canceled_source_unchanged", "canceled_recovered_to_tray"}


def test_unknown_controller_feedback_state_is_not_forwarded():
    bridge = _bare_bridge()
    command = _skill()
    _activate_tool_transfer(bridge, command)
    statuses = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_tool_transfer_feedback(
        command,
        SimpleNamespace(
            feedback=SimpleNamespace(state="robot_vendor_step_17", progress=0.4)
        ),
    )

    assert statuses == [
        {
            "state": "fault",
            "success": False,
            "reason_code": "invalid_controller_feedback_state",
            "progress": 0.4,
        }
    ]


def test_cancel_recovery_feedback_remains_visible_until_terminal_result():
    bridge = _bare_bridge()
    command = _skill()
    _activate_tool_transfer(bridge, command, cancel_requested=True)
    statuses = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_tool_transfer_feedback(
        command,
        SimpleNamespace(
            feedback=SimpleNamespace(state="recovering_to_tray", progress=0.8)
        ),
    )

    assert statuses == [
        {
            "state": "recovering_to_tray",
            "success": False,
            "reason_code": "cancel_recovery",
            "progress": 0.8,
        }
    ]


def test_stop_detaches_ui_but_retains_controller_recovery_records():
    bridge = _bare_bridge()
    skill = _skill()
    service_group = _group()
    goal_handle = _GoalHandle()
    bridge._active_actions = {
        ("tool_transfer", skill.command_id): ActiveAction(
            route="tool_transfer",
            command=skill,
            goal_handle=goal_handle,
            dispatched=True,
        )
    }
    bridge._active_services = {
        ("retraction", service_group.command_id): ActiveService(
            route="retraction", command=service_group, dispatched=True
        )
    }
    statuses = []
    traces = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        ("skill", command.command_id, kwargs)
    )
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(
        ("group", command.command_id, kwargs)
    )
    bridge._publish_execution_trace = lambda **kwargs: traces.append(kwargs)
    assert bridge._dispatch_ledger.reserve("prior-run-command")

    bridge._on_control(SimpleNamespace(data="stop:operator"))

    assert not bridge._runtime_accepting_commands
    assert goal_handle.cancel_calls == 1
    assert set(bridge._active_actions) == {("tool_transfer", skill.command_id)}
    assert set(bridge._active_services) == {("retraction", service_group.command_id)}
    assert not bridge._dispatch_ledger.reserve("prior-run-command")
    assert bridge._begin_action_dispatch("tool_transfer", _skill("after-stop")) == (
        "runtime_not_accepting_commands"
    )
    assert ("skill", "skill-1", {
        "state": "cancel_requested",
        "success": False,
        "reason_code": "controller_recovery_pending_after_stop",
    }) in statuses
    assert ("group", service_group.command_id, {
        "state": "unknown",
        "outcome": "controller_recovery_pending",
        "terminal": False,
        "success": False,
        "reason_code": "controller_recovery_pending_after_stop",
    }) in statuses
    assert {trace["command_id"] for trace in traces} == {
        skill.command_id,
        service_group.command_id,
    }
    assert all(trace["terminal"] is False for trace in traces)


def test_clean_start_keeps_prior_dispatched_action_as_controller_lane_blocker():

    bridge = _bare_bridge()
    bridge._publish_execution_route_state = lambda: None
    bridge._publish_skill_status = lambda *_args, **_kwargs: None
    prior = _skill("prior-run-handover")
    prior_goal = _GoalHandle()
    bridge._active_actions[("tool_transfer", prior.command_id)] = ActiveAction(
        route="tool_transfer",
        command=prior,
        goal_handle=prior_goal,
        dispatched=True,
        dispatch_epoch=0,
    )

    bridge._on_control(SimpleNamespace(data="stop"))
    assert prior_goal.cancel_calls == 1
    assert set(bridge._active_actions) == {("tool_transfer", prior.command_id)}

    bridge._on_control(SimpleNamespace(data="start"))
    next_command = _skill("next-run-handover")

    assert bridge._begin_action_dispatch("tool_transfer", next_command) == (
        "tool_transfer_busy"
    )
    assert set(bridge._active_actions) == {("tool_transfer", prior.command_id)}


def test_clean_start_keeps_prior_service_as_controller_lane_blocker():

    bridge = _bare_bridge()
    bridge._publish_group_status = lambda *_args, **_kwargs: None
    prior = _group("prior-run-retraction")
    bridge._active_services[("retraction", prior.command_id)] = ActiveService(
        route="retraction",
        command=prior,
        dispatched=True,
        dispatch_epoch=0,
    )

    bridge._on_control(SimpleNamespace(data="stop"))
    bridge._on_control(SimpleNamespace(data="start"))
    next_command = _group("next-run-retraction")

    assert bridge._begin_service_dispatch("retraction", next_command) == (
        "retraction_busy"
    )
    assert set(bridge._active_services) == {("retraction", prior.command_id)}


def test_controller_recovery_timeout_releases_only_explicitly_unknown_records(
    monkeypatch: pytest.MonkeyPatch,
):
    bridge = _bare_bridge()
    bridge._controller_recovery_timeout_sec = 15.0
    action = _skill("expired-action")
    service = _group("expired-service")
    bridge._active_actions[("tool_transfer", action.command_id)] = ActiveAction(
        route="tool_transfer",
        command=action,
        cancelled=True,
        dispatched=True,
        recovery_deadline_monotonic=5.0,
    )
    bridge._active_services[("retraction", service.command_id)] = ActiveService(
        route="retraction",
        command=service,
        cancelled=True,
        dispatched=True,
        recovery_deadline_monotonic=5.0,
    )
    traces: list[dict[str, object]] = []
    bridge._publish_execution_trace = lambda **kwargs: traces.append(kwargs)
    bridge._publish_execution_route_state = lambda: None
    bridge.get_logger = lambda: SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: 6.0)

    bridge._expire_controller_recovery()

    assert bridge._active_actions == {}
    assert bridge._active_services == {}
    assert {trace["reason_code"] for trace in traces} == {
        "controller_recovery_timeout"
    }
    assert all(trace["stage"] == "unknown" for trace in traces)
    assert all(trace["terminal"] is True for trace in traces)


def test_stale_action_terminal_is_not_projected_into_the_restarted_run():
    bridge = _bare_bridge()
    command = _skill("prior-run-terminal")
    bridge._dispatch_epoch = 1
    bridge._active_actions[("tool_transfer", command.command_id)] = ActiveAction(
        route="tool_transfer",
        command=command,
        cancelled=True,
        dispatch_epoch=0,
    )
    statuses = []
    traces = []
    bridge._publish_skill_status = lambda *_args, **kwargs: statuses.append(kwargs)
    bridge._publish_execution_trace = lambda **kwargs: traces.append(kwargs)

    bridge._on_tool_transfer_result(
        command,
        SimpleNamespace(result=lambda: SimpleNamespace(status=GoalStatus.STATUS_CANCELED)),
    )

    assert ("tool_transfer", command.command_id) not in bridge._active_actions
    assert statuses == []
    assert traces == []


def test_stopped_action_callback_cannot_release_a_reused_command_id():
    bridge = _bare_bridge()
    command = _skill("reused-handover")
    bridge._dispatch_epoch = 1
    current = ActiveAction(
        route="tool_transfer",
        command=command,
        dispatch_epoch=1,
    )
    bridge._active_actions[("tool_transfer", command.command_id)] = current
    statuses = []
    bridge._publish_skill_status = lambda *_args, **kwargs: statuses.append(kwargs)

    bridge._on_tool_transfer_result(
        command,
        SimpleNamespace(result=lambda: object()),
        expected_epoch=0,
    )

    assert bridge._active_actions[("tool_transfer", command.command_id)] is current
    assert statuses == []


def test_stale_retraction_receipt_is_not_projected_into_the_restarted_run():
    bridge = _bare_bridge()
    command = _group("prior-run-retraction-terminal")
    bridge._dispatch_epoch = 1
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction",
        command=command,
        cancelled=True,
        dispatched=True,
        dispatch_epoch=0,
    )
    statuses = []
    traces = []
    bridge._publish_group_status = lambda *_args, **kwargs: statuses.append(kwargs)
    bridge._publish_execution_trace = lambda **kwargs: traces.append(kwargs)

    bridge._on_retraction_service_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                request_accepted=True,
                result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
                command_id=command.command_id,
                message="controller_received",
            )
        ),
    )

    assert ("retraction", command.command_id) not in bridge._active_services
    assert bridge._runtime_is_accepting()
    assert statuses == []
    assert traces == []


def test_stopped_service_callback_cannot_release_a_reused_command_id():
    bridge = _bare_bridge()
    command = _group("reused-retraction")
    bridge._dispatch_epoch = 1
    current = ActiveService(
        route="retraction",
        command=command,
        dispatched=True,
        dispatch_epoch=1,
    )
    bridge._active_services[("retraction", command.command_id)] = current
    statuses = []
    bridge._publish_group_status = lambda *_args, **kwargs: statuses.append(kwargs)

    bridge._on_retraction_service_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                request_accepted=True,
                result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
                command_id=command.command_id,
                message="accepted",
            )
        ),
        expected_epoch=0,
    )

    assert bridge._active_services[("retraction", command.command_id)] is current
    assert statuses == []


def test_retraction_service_acceptance_after_stop_preserves_unknown_physical_state():
    bridge = _bare_bridge()
    command = _group("adjust-cancel-1")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction",
        command=command,
        cancelled=True,
        dispatched=True,
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_retraction_service_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                request_accepted=True,
                result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
                command_id=command.command_id,
                message="controller_received",
            )
        ),
    )

    assert ("retraction", command.command_id) not in bridge._active_services
    assert not bridge._runtime_is_accepting()
    assert statuses == [{
        "state": "unknown",
        "outcome": "accepted_after_stop",
        "terminal": False,
        "success": False,
        "reason_code": "controller_received",
    }]


def test_retraction_service_response_loss_keeps_lane_locked_and_nonterminal():
    bridge = _bare_bridge()
    command = _group("adjust-response-lost")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction", command=command
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    def _raise_transport_error():
        raise RuntimeError("service response lost")

    bridge._on_retraction_service_result(
        command, SimpleNamespace(result=_raise_transport_error)
    )

    assert ("retraction", command.command_id) in bridge._active_services
    assert not bridge._runtime_is_accepting()
    assert statuses == [{
        "state": "unknown",
        "outcome": "remote_state_unknown",
        "terminal": False,
        "success": False,
        "reason_code": "service_response_unavailable",
    }]


def test_tool_transfer_goal_response_loss_keeps_lane_locked():
    bridge = _bare_bridge()
    command = _skill("handover-response-lost")
    _activate_tool_transfer(bridge, command)
    statuses = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)

    def _raise_transport_error():
        raise RuntimeError("goal response lost")

    bridge._on_tool_transfer_goal_response(
        command, SimpleNamespace(result=_raise_transport_error)
    )

    assert ("tool_transfer", command.command_id) in bridge._active_actions
    assert not bridge._runtime_is_accepting()
    assert statuses == [{
        "state": "unknown",
        "success": False,
        "reason_code": "goal_response_unavailable",
        "progress": 0.0,
    }]


def test_cancelled_goal_rejection_rebases_voice_from_confirmed_source() -> None:
    bridge = _bare_bridge()
    active = _skill("handover-rejected-after-cancel")
    _activate_tool_transfer(bridge, active, semantic_leg=("tray", "surgeon"))
    bridge._publish_skill_status = lambda *_args, **_kwargs: None
    bridge._publish_execution_trace = lambda **_kwargs: None
    voice = _voice_skill("voice-after-goal-rejection", 64)
    assert bridge._queue_voice_tool_transfer_preemption(
        voice,
        _tool_transfer_request(voice),
    )
    dispatched: list[ToolHandoverRequest] = []
    bridge._dispatch_reserved_tool_transfer = (
        lambda _command, request, **_kwargs: dispatched.append(request)
    )

    bridge._on_tool_transfer_goal_response(
        active,
        SimpleNamespace(result=lambda: SimpleNamespace(accepted=False)),
    )

    assert len(dispatched) == 1
    assert dispatched[0].source_location == "tray"
    assert dispatched[0].target_location == "surgeon"


def test_retraction_service_admission_is_transport_terminal_not_physical_completion():
    bridge = _bare_bridge()
    command = _tool_change_group("change-admitted")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction",
        command=command,
        dispatched=True,
        future=object(),
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_retraction_service_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                request_accepted=True,
                result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
                command_id=command.command_id,
                message="accepted_for_controller_execution",
            )
        ),
    )

    assert ("retraction", command.command_id) not in bridge._active_services
    assert statuses == [{
        "state": "accepted",
        "outcome": "accepted",
        "terminal": True,
        "success": True,
        "reason_code": "accepted_for_controller_execution",
    }]


def test_missing_retraction_service_response_after_stop_remains_nonterminal_and_tracked():
    bridge = _bare_bridge()
    command = _tool_change_group("change-unknown")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction",
        command=command,
        cancelled=True,
        dispatched=True,
        future=object(),
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_retraction_service_result(
        command,
        SimpleNamespace(result=lambda: None),
    )

    assert ("retraction", command.command_id) in bridge._active_services
    assert not bridge._runtime_is_accepting()
    assert statuses == [{
        "state": "unknown",
        "outcome": "remote_state_unknown",
        "terminal": False,
        "success": False,
        "reason_code": "service_response_unavailable_after_stop",
    }]


def test_invalid_retraction_service_response_remains_nonterminal_and_tracked():
    bridge = _bare_bridge()
    command = _group("adjust-unknown")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction",
        command=command,
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_retraction_service_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                request_accepted=True,
                result_code=ExecuteRetractionCommand.Response.RESULT_REJECTED,
                command_id=command.command_id,
                message="inconsistent",
            )
        ),
    )

    assert ("retraction", command.command_id) in bridge._active_services
    assert not bridge._runtime_is_accepting()
    assert statuses == [{
        "state": "unknown",
        "outcome": "remote_state_unknown",
        "terminal": False,
        "success": False,
        "reason_code": "invalid_service_response",
    }]


def test_tool_change_admission_status_never_claims_physical_attachment():
    bridge = _bare_bridge()
    command = _tool_change_group()
    published = []
    bridge._stamp = lambda: Time()
    bridge._group_status_pub = SimpleNamespace(publish=published.append)

    bridge._publish_group_status(
        command,
        state="accepted",
        outcome="accepted",
        terminal=True,
        success=True,
        reason_code="request_accepted",
    )

    assert published[0].target_tool_id == "army_navy_retractor"
    assert published[0].end_effector_profile == ""


def test_retraction_service_request_uses_only_the_reviewed_fields():
    bridge = _bare_bridge()
    service_request = bridge._retraction_service_request(
        _retraction_request("service-request-1", distance_m=0.050)
    )

    assert service_request.protocol_version == 1
    assert service_request.source_id == "taskplanner-test"
    assert service_request.command_id == "service-request-1"
    assert (
        service_request.command
        == ExecuteRetractionCommand.Request.COMMAND_ADJUST_RETRACTION
    )
    assert service_request.target_side == ExecuteRetractionCommand.Request.TARGET_LEFT
    assert service_request.distance_m == 0.050


def test_active_command_id_remains_deduplicated_even_after_ledger_eviction():
    bridge = _bare_bridge()
    active = _skill("active-1")
    bridge._active_actions = {
        ("tool_transfer", active.command_id): ActiveAction(
            route="tool_transfer", command=active
        )
    }
    bridge._dispatch_ledger = DispatchLedger(max_entries=1)
    assert bridge._dispatch_ledger.reserve("older-command")
    assert bridge._dispatch_ledger.reserve("newer-command")

    assert bridge._begin_action_dispatch("tool_transfer", active) == "duplicate_command"


def test_next_tool_goal_is_blocked_while_cancel_recovery_is_active():
    bridge = _bare_bridge()
    active = _skill("active-1")
    _activate_tool_transfer(bridge, active, cancel_requested=True)

    assert (
        bridge._begin_action_dispatch(
            "tool_transfer",
            replace(_skill("next-1"), request_generation=5),
        )
        == "tool_transfer_busy"
    )


def _voice_skill(command_id: str, generation: int) -> InternalSkillCommand:
    return replace(
        _skill(command_id),
        request_generation=generation,
        mode="explicit_request",
        voice_backed=True,
    )


def _tool_transfer_request(command: InternalSkillCommand) -> ToolHandoverRequest:
    return ToolHandoverRequest(
        command_id=command.command_id,
        instrument_id="Adson forceps",
        instrument_instance_id="Adson forceps#1",
        source_location="tray",
        target_location="surgeon",
    )


def test_skill_message_voice_provenance_is_backward_safe_and_explicit() -> None:
    command = _skill("voice-wire")
    fields = {
        name: getattr(command, name)
        for name in command.__dataclass_fields__
        if name != "voice_backed"
    }

    assert not SurgicalInteropExecutionBridge._skill_from_msg(
        SimpleNamespace(**fields)
    ).voice_backed
    assert SurgicalInteropExecutionBridge._skill_from_msg(
        SimpleNamespace(**fields, voice_backed=True)
    ).voice_backed


def test_live_voice_ingress_never_cancels_or_replaces_an_active_tool_action() -> None:
    bridge = _bare_bridge()
    active = _skill("active-handover")
    goal_handle = _GoalHandle()
    _activate_tool_transfer(bridge, active, goal_handle=goal_handle)
    incoming = _voice_skill("voice-while-active", 31)
    bridge._skill_from_msg = lambda _message: incoming
    bridge._direct_hand_run_guard = lambda _command: ""
    bridge._public_instrument_identity = lambda _command: (
        "Adson forceps",
        "Adson forceps#1",
    )
    bridge._tool_transfer_client = SimpleNamespace(
        wait_for_server=lambda *, timeout_sec: True
    )
    bridge._server_wait_timeout_sec = 0.0
    statuses = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        (command.command_id, kwargs)
    )

    bridge._on_skill(SimpleNamespace())

    assert goal_handle.cancel_calls == 0
    assert bridge._queued_voice_tool_transfer is None
    assert statuses == [
        (
            "voice-while-active",
            {
                "state": "busy",
                "success": False,
                "reason_code": "tool_transfer_busy",
            },
        )
    ]


def test_latest_voice_preempts_once_and_non_voice_cannot_replace_queue() -> None:
    bridge = _bare_bridge()
    active = _skill("active-handover")
    goal_handle = _GoalHandle()
    _activate_tool_transfer(bridge, active, goal_handle=goal_handle)
    statuses: list[tuple[str, dict[str, object]]] = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        (command.command_id, kwargs)
    )

    first_voice = _voice_skill("voice-1", 21)
    assert bridge._queue_voice_tool_transfer_preemption(
        first_voice, _tool_transfer_request(first_voice)
    )
    assert goal_handle.cancel_calls == 1
    assert bridge._queued_voice_tool_transfer.command.command_id == "voice-1"
    assert bridge._active_actions[("tool_transfer", active.command_id)].cancelled

    implicit = replace(_skill("implicit-1"), mode="implicit_vlm", voice_backed=False)
    assert not bridge._queue_voice_tool_transfer_preemption(
        implicit, _tool_transfer_request(implicit)
    )
    assert bridge._begin_action_dispatch("tool_transfer", implicit) == "tool_transfer_busy"
    assert bridge._queued_voice_tool_transfer.command.command_id == "voice-1"

    latest_voice = _voice_skill("voice-2", 22)
    assert bridge._queue_voice_tool_transfer_preemption(
        latest_voice, _tool_transfer_request(latest_voice)
    )
    assert goal_handle.cancel_calls == 1
    assert bridge._queued_voice_tool_transfer.command.command_id == "voice-2"
    assert (
        "voice-1",
        {
            "state": "cancelled",
            "success": False,
            "reason_code": "superseded_by_newer_voice_request",
        },
    ) in statuses


def test_replayed_voice_generation_does_not_interrupt_or_replace_latest_queue() -> None:
    bridge = _bare_bridge()
    active = _skill("active-handover")
    goal_handle = _GoalHandle()
    _activate_tool_transfer(bridge, active, goal_handle=goal_handle)
    statuses: list[tuple[str, dict[str, object]]] = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        (command.command_id, kwargs)
    )

    latest = _voice_skill("voice-latest", 31)
    replay = _voice_skill("voice-replay", 31)
    assert bridge._queue_voice_tool_transfer_preemption(
        latest, _tool_transfer_request(latest)
    )
    assert bridge._queue_voice_tool_transfer_preemption(
        replay, _tool_transfer_request(replay)
    )

    assert goal_handle.cancel_calls == 1
    assert bridge._queued_voice_tool_transfer.command.command_id == "voice-latest"
    assert statuses[-1] == (
        "voice-replay",
        {
            "state": "duplicate_suppressed",
            "success": False,
            "reason_code": "duplicate_voice_preemption_request",
        },
    )


def test_next_semantic_leg_in_same_generation_never_cancels_active_goal() -> None:
    bridge = _bare_bridge()
    active = _voice_skill("prepare-adson", 32)
    active_request = replace(
        _tool_transfer_request(active),
        command_id=active.command_id,
        source_location="mayo",
        target_location="robot",
    )
    goal_handle = _GoalHandle()
    _activate_tool_transfer(
        bridge,
        active,
        goal_handle=goal_handle,
        semantic_leg=bridge._tool_transfer_semantic_leg(active_request),
    )
    assert bridge._dispatch_ledger.reserve(
        active.command_id,
        explicit_request_generation=32,
        semantic_leg=bridge._tool_transfer_semantic_leg(active_request),
    )

    handover = _voice_skill("handover-adson", 32)
    handover_request = replace(
        _tool_transfer_request(handover),
        command_id=handover.command_id,
        source_location="robot",
        target_location="surgeon",
    )
    handover_leg = bridge._tool_transfer_semantic_leg(handover_request)

    assert not bridge._queue_voice_tool_transfer_preemption(
        handover,
        handover_request,
    )
    assert goal_handle.cancel_calls == 0
    assert bridge._queued_voice_tool_transfer is None
    assert not bridge._active_actions[("tool_transfer", active.command_id)].cancelled
    assert (
        bridge._begin_action_dispatch(
            "tool_transfer",
            handover,
            semantic_leg=handover_leg,
        )
        == "tool_transfer_busy"
    )

    bridge._clear_action("tool_transfer", active.command_id)
    assert (
        bridge._begin_action_dispatch(
            "tool_transfer",
            handover,
            semantic_leg=handover_leg,
        )
        == ""
    )
    bridge._clear_action("tool_transfer", handover.command_id)
    assert (
        bridge._begin_action_dispatch(
            "tool_transfer",
            _voice_skill("handover-adson-replay", 32),
            semantic_leg=handover_leg,
        )
        == "duplicate_command"
    )


def test_same_tool_voice_waits_for_auto_return_then_defers_mayo_pickup_to_planner() -> None:
    bridge = _bare_bridge()
    active = replace(
        _skill("return-bipolar"),
        action="return_unused_preposition",
        source_location_type="robot_right_hand",
        source_location_id="robot_right_hand",
        target_location_type="mayo_stand",
        target_location_id="mayo_stand",
        instrument_id="T07",
        instrument_instance_id="T07#1",
        mode="anticipatory",
        voice_backed=False,
    )
    goal_handle = _GoalHandle()
    _activate_tool_transfer(
        bridge,
        active,
        goal_handle=goal_handle,
        semantic_leg=("robot", "mayo"),
    )
    statuses: list[tuple[str, dict[str, object]]] = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        (command.command_id, kwargs)
    )
    bridge._publish_tool_transfer_completed_events = lambda *_args, **_kwargs: None
    bridge._publish_execution_trace = lambda **_kwargs: None

    voice = replace(
        _voice_skill("voice-bipolar", 52),
        instrument_id="T07",
        instrument_instance_id="T07#1",
    )
    voice_request = replace(
        _tool_transfer_request(voice),
        command_id=voice.command_id,
        instrument_id="Bipolar forceps",
        instrument_instance_id="Bipolar forceps#1",
        source_location="robot",
        target_location="surgeon",
    )

    assert bridge._queue_voice_tool_transfer_preemption(voice, voice_request)
    assert goal_handle.cancel_calls == 0
    assert not bridge._active_actions[("tool_transfer", active.command_id)].cancelled
    assert bridge._queued_voice_tool_transfer.wait_for_predecessor_terminal
    assert statuses[-1] == (
        voice.command_id,
        {
            "state": "queued",
            "success": True,
            "reason_code": "waiting_for_active_auto_return_terminal",
        },
    )

    dispatched: list[tuple[InternalSkillCommand, ToolHandoverRequest]] = []

    def dispatch(command, request, *, expected_epoch, server_ready) -> None:
        assert list(bridge._active_actions) == [
            ("tool_transfer", voice.command_id)
        ]
        assert expected_epoch == 0
        assert not server_ready
        dispatched.append((command, request))

    bridge._dispatch_reserved_tool_transfer = dispatch
    bridge._on_tool_transfer_result(
        active,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                status=GoalStatus.STATUS_SUCCEEDED,
                result=SimpleNamespace(
                    success=True,
                    final_state=ExecuteToolHandover.Result.FINAL_COMPLETED,
                    reason_code="completed",
                ),
            )
        ),
    )

    assert dispatched == []
    assert statuses[-1] == (
        voice.command_id,
        {
            "state": "pending",
            "success": True,
            "reason_code": "mayo_source_requires_fresh_planner_admission",
        },
    )
    assert bridge._runtime_is_accepting()


def test_same_tool_voice_uses_robot_source_after_external_source_unchanged_cancel() -> None:
    bridge = _bare_bridge()
    active = replace(
        _skill("return-source-unchanged"),
        action="return_unused_preposition",
        source_location_type="robot_right_hand",
        source_location_id="robot_right_hand",
        target_location_type="tray_slot",
        target_location_id="main_tray_slot_4",
        mode="anticipatory",
        voice_backed=False,
    )
    _activate_tool_transfer(
        bridge,
        active,
        goal_handle=_GoalHandle(),
        semantic_leg=("robot", "tray"),
    )
    bridge._publish_skill_status = lambda *_args, **_kwargs: None
    bridge._publish_execution_trace = lambda **_kwargs: None
    bridge._publish_tool_transfer_cancel_reconciliation = (
        lambda *_args, **_kwargs: None
    )
    voice = _voice_skill("voice-source-unchanged", 53)
    voice_request = replace(
        _tool_transfer_request(voice),
        command_id=voice.command_id,
        source_location="robot",
        target_location="surgeon",
    )
    assert bridge._queue_voice_tool_transfer_preemption(voice, voice_request)
    dispatched: list[tuple[InternalSkillCommand, ToolHandoverRequest]] = []
    bridge._dispatch_reserved_tool_transfer = (
        lambda command, request, **_kwargs: dispatched.append((command, request))
    )

    bridge._on_tool_transfer_result(
        active,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                status=GoalStatus.STATUS_CANCELED,
                result=SimpleNamespace(
                    success=False,
                    final_state=ExecuteToolHandover.Result.FINAL_CANCELED,
                    reason_code=(
                        ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED
                    ),
                ),
            )
        ),
    )

    assert len(dispatched) == 1
    deferred_command, deferred_request = dispatched[0]
    assert deferred_command.action == "direct_handover"
    assert deferred_request.source_location == "robot"
    assert deferred_request.target_location == "surgeon"


def test_different_tool_voice_still_preempts_an_active_auto_return() -> None:
    bridge = _bare_bridge()
    active = replace(
        _skill("return-adson"),
        action="return_unused_preposition",
        mode="anticipatory",
        voice_backed=False,
    )
    goal_handle = _GoalHandle()
    _activate_tool_transfer(bridge, active, goal_handle=goal_handle)
    bridge._publish_skill_status = lambda *_args, **_kwargs: None
    voice = replace(
        _voice_skill("voice-other-tool", 54),
        instrument_id="T07",
        instrument_instance_id="T07#1",
    )
    voice_request = replace(
        _tool_transfer_request(voice),
        command_id=voice.command_id,
        instrument_id="Bipolar forceps",
        instrument_instance_id="Bipolar forceps#1",
        source_location="tray",
        target_location="surgeon",
    )

    assert bridge._queue_voice_tool_transfer_preemption(voice, voice_request)
    assert goal_handle.cancel_calls == 1
    assert bridge._active_actions[("tool_transfer", active.command_id)].cancelled
    assert not bridge._queued_voice_tool_transfer.wait_for_predecessor_terminal


def test_safe_cancel_result_dispatches_latest_voice_only_after_lane_is_clear() -> None:
    bridge = _bare_bridge()
    active = _skill("active-handover")
    _activate_tool_transfer(bridge, active, semantic_leg=("tray", "surgeon"))
    bridge._publish_skill_status = lambda *_args, **_kwargs: None
    bridge._stamp = lambda: Time()
    voice = _voice_skill("voice-after-cancel", 41)
    assert bridge._queue_voice_tool_transfer_preemption(
        voice, _tool_transfer_request(voice)
    )
    observed: list[tuple[str, object]] = []
    bridge._skill_event_pub = SimpleNamespace(
        publish=lambda event: observed.append(("reconcile", event))
    )

    def dispatch(command, _request, **_kwargs) -> None:
        assert list(bridge._active_actions) == [
            ("tool_transfer", voice.command_id)
        ]
        assert bridge._queued_voice_tool_transfer is None
        observed.append(("dispatch", command.command_id))

    bridge._dispatch_reserved_tool_transfer = dispatch
    bridge._on_tool_transfer_result(
        active,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                status=GoalStatus.STATUS_CANCELED,
                result=SimpleNamespace(
                    success=False,
                    final_state=ExecuteToolHandover.Result.FINAL_CANCELED,
                    reason_code=(
                        ExecuteToolHandover.Result.REASON_CANCELED_RECOVERED_TO_TRAY
                    ),
                ),
            )
        ),
    )

    assert [kind for kind, _value in observed] == ["reconcile", "dispatch"]
    event = observed[0][1]
    assert event.event_type == "UnusedPrepositionReturned"
    assert event.status == "returned"
    assert event.source_location_type == "robot"
    assert event.source_location_id == "robot"
    assert event.target_location_type == "tray"
    assert event.target_location_id == "tray"
    assert json.loads(event.detail_json) == {
        "command_id": "active-handover",
        "controller_final_state": "canceled",
        "controller_reason_code": "canceled_recovered_to_tray",
        "request_generation": 4,
        "voice_backed": False,
    }


def test_source_unchanged_cancel_preserves_inventory_before_voice_dispatch() -> None:
    bridge = _bare_bridge()
    active = _skill("active-source-unchanged")
    _activate_tool_transfer(bridge, active, semantic_leg=("tray", "surgeon"))
    bridge._publish_skill_status = lambda *_args, **_kwargs: None
    bridge._stamp = lambda: Time()
    inventory_events: list[object] = []
    bridge._skill_event_pub = SimpleNamespace(publish=inventory_events.append)
    voice = _voice_skill("voice-after-source-unchanged", 45)
    assert bridge._queue_voice_tool_transfer_preemption(
        voice, _tool_transfer_request(voice)
    )
    dispatched: list[str] = []
    bridge._dispatch_reserved_tool_transfer = (
        lambda command, _request, **_kwargs: dispatched.append(command.command_id)
    )

    bridge._on_tool_transfer_result(
        active,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                status=GoalStatus.STATUS_CANCELED,
                result=SimpleNamespace(
                    success=False,
                    final_state=ExecuteToolHandover.Result.FINAL_CANCELED,
                    reason_code=(
                        ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED
                    ),
                ),
            )
        ),
    )

    assert inventory_events == []
    assert dispatched == ["voice-after-source-unchanged"]


def test_completed_same_tool_handover_satisfies_voice_without_stale_redispatch() -> None:
    bridge = _bare_bridge()
    active = _skill("active-won-race")
    _activate_tool_transfer(bridge, active, semantic_leg=("tray", "surgeon"))
    statuses: list[tuple[str, dict[str, object]]] = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        (command.command_id, kwargs)
    )
    bridge._publish_tool_transfer_completed_events = lambda *_args, **_kwargs: None
    voice = _voice_skill("voice-after-completion", 42)
    assert bridge._queue_voice_tool_transfer_preemption(
        voice, _tool_transfer_request(voice)
    )
    dispatched: list[str] = []
    bridge._dispatch_reserved_tool_transfer = (
        lambda command, _request, **_kwargs: dispatched.append(command.command_id)
    )

    bridge._on_tool_transfer_result(
        active,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                status=GoalStatus.STATUS_SUCCEEDED,
                result=SimpleNamespace(
                    success=True,
                    final_state=ExecuteToolHandover.Result.FINAL_COMPLETED,
                    reason_code="completed",
                ),
            )
        ),
    )

    assert dispatched == []
    assert statuses[-1] == (
        "voice-after-completion",
        {
            "state": "duplicate_suppressed",
            "success": True,
            "reason_code": "voice_request_satisfied_by_predecessor",
        },
    )
    assert bridge._dispatch_ledger.is_reserved(
        "delayed-voice-after-completion",
        explicit_request_generation=42,
        semantic_leg=("tray", "surgeon"),
    )


def test_same_tool_cancel_recovery_rebases_and_fences_original_and_effective_legs() -> None:
    bridge = _bare_bridge()
    active = replace(
        _skill("active-robot-handover"),
        action="direct_handover",
        source_location_type="robot_right_hand",
        source_location_id="robot_right_hand",
    )
    _activate_tool_transfer(bridge, active, semantic_leg=("robot", "surgeon"))
    bridge._publish_skill_status = lambda *_args, **_kwargs: None
    bridge._publish_execution_trace = lambda **_kwargs: None
    bridge._publish_tool_transfer_cancel_reconciliation = (
        lambda *_args, **_kwargs: None
    )
    voice = _voice_skill("voice-rebased-from-recovery", 71)
    original_request = replace(
        _tool_transfer_request(voice),
        source_location="robot",
        target_location="surgeon",
    )
    assert bridge._queue_voice_tool_transfer_preemption(voice, original_request)

    dispatched: list[tuple[InternalSkillCommand, ToolHandoverRequest]] = []
    bridge._dispatch_reserved_tool_transfer = (
        lambda command, request, **_kwargs: dispatched.append((command, request))
    )
    bridge._on_tool_transfer_result(
        active,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                status=GoalStatus.STATUS_CANCELED,
                result=SimpleNamespace(
                    success=False,
                    final_state=ExecuteToolHandover.Result.FINAL_CANCELED,
                    reason_code=(
                        ExecuteToolHandover.Result.REASON_CANCELED_RECOVERED_TO_TRAY
                    ),
                ),
            )
        ),
    )

    assert len(dispatched) == 1
    command, request = dispatched[0]
    assert command.action == "pick_up_and_handover"
    assert request.source_location == "tray"
    assert request.target_location == "surgeon"
    for delayed_id, leg in (
        ("delayed-original", ("robot", "surgeon")),
        ("delayed-effective", ("tray", "surgeon")),
    ):
        assert bridge._dispatch_ledger.is_reserved(
            delayed_id,
            explicit_request_generation=71,
            semantic_leg=leg,
        )


def test_prepare_completion_rebases_voice_from_robot_and_fences_tray_origin() -> None:
    bridge = _bare_bridge()
    active = replace(
        _skill("active-prepare"),
        action="predict_tool",
        target_location_type="robot_right_hand",
        target_location_id="robot_right_hand",
        mode="anticipatory",
        voice_backed=False,
    )
    _activate_tool_transfer(bridge, active, semantic_leg=("tray", "robot"))
    bridge._publish_skill_status = lambda *_args, **_kwargs: None
    bridge._publish_execution_trace = lambda **_kwargs: None
    bridge._publish_tool_transfer_completed_events = lambda *_args, **_kwargs: None
    voice = _voice_skill("voice-after-prepare", 72)
    original_request = _tool_transfer_request(voice)
    assert bridge._queue_voice_tool_transfer_preemption(voice, original_request)

    dispatched: list[tuple[InternalSkillCommand, ToolHandoverRequest]] = []
    bridge._dispatch_reserved_tool_transfer = (
        lambda command, request, **_kwargs: dispatched.append((command, request))
    )
    bridge._on_tool_transfer_result(
        active,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                status=GoalStatus.STATUS_SUCCEEDED,
                result=SimpleNamespace(
                    success=True,
                    final_state=ExecuteToolHandover.Result.FINAL_COMPLETED,
                    reason_code="completed",
                ),
            )
        ),
    )

    assert len(dispatched) == 1
    command, request = dispatched[0]
    assert command.action == "direct_handover"
    assert request.source_location == "robot"
    assert request.target_location == "surgeon"
    for delayed_id, leg in (
        ("delayed-tray-origin", ("tray", "surgeon")),
        ("delayed-robot-rebase", ("robot", "surgeon")),
    ):
        assert bridge._dispatch_ledger.is_reserved(
            delayed_id,
            explicit_request_generation=72,
            semantic_leg=leg,
        )


def test_failed_predecessor_drops_voice_queue_and_keeps_dispatch_blocked() -> None:
    bridge = _bare_bridge()
    active = _skill("active-failed")
    _activate_tool_transfer(bridge, active)
    statuses: list[tuple[str, dict[str, object]]] = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        (command.command_id, kwargs)
    )
    voice = _voice_skill("voice-not-safe", 43)
    assert bridge._queue_voice_tool_transfer_preemption(
        voice, _tool_transfer_request(voice)
    )
    bridge._dispatch_reserved_tool_transfer = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("unsafe predecessor dispatched queued voice")
    )

    bridge._on_tool_transfer_result(
        active,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                status=GoalStatus.STATUS_ABORTED,
                result=SimpleNamespace(
                    success=False,
                    final_state=ExecuteToolHandover.Result.FINAL_FAILED,
                    reason_code="recovery_failed",
                ),
            )
        ),
    )

    assert not bridge._runtime_accepting_commands
    assert bridge._queued_voice_tool_transfer is None
    assert statuses[-1] == (
        "voice-not-safe",
        {
            "state": "rejected",
            "success": False,
            "reason_code": "voice_preemption_predecessor_not_safely_terminal",
        },
    )


def test_ambiguous_cancel_never_mutates_inventory_or_dispatches_queued_voice() -> None:
    bridge = _bare_bridge()
    active = _skill("active-ambiguous-cancel")
    _activate_tool_transfer(bridge, active)
    statuses: list[tuple[str, dict[str, object]]] = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        (command.command_id, kwargs)
    )
    inventory_events: list[object] = []
    bridge._skill_event_pub = SimpleNamespace(publish=inventory_events.append)
    voice = _voice_skill("voice-after-ambiguous-cancel", 46)
    assert bridge._queue_voice_tool_transfer_preemption(
        voice, _tool_transfer_request(voice)
    )
    bridge._dispatch_reserved_tool_transfer = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("ambiguous Cancel dispatched queued voice")
    )

    bridge._on_tool_transfer_result(
        active,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                status=GoalStatus.STATUS_CANCELED,
                result=SimpleNamespace(
                    success=False,
                    final_state=ExecuteToolHandover.Result.FINAL_CANCELED,
                    reason_code="operator_cancel",
                ),
            )
        ),
    )

    assert inventory_events == []
    assert not bridge._runtime_is_accepting()
    assert bridge._queued_voice_tool_transfer is None
    assert statuses[-1] == (
        "voice-after-ambiguous-cancel",
        {
            "state": "rejected",
            "success": False,
            "reason_code": "voice_preemption_predecessor_not_safely_terminal",
        },
    )


def test_runtime_stop_discards_queued_voice_before_cancel_result() -> None:
    bridge = _bare_bridge()
    active = _skill("active-stop")
    _activate_tool_transfer(bridge, active)
    statuses: list[tuple[str, dict[str, object]]] = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        (command.command_id, kwargs)
    )
    bridge._publish_execution_route_state = lambda: None
    voice = _voice_skill("voice-stopped", 44)
    assert bridge._queue_voice_tool_transfer_preemption(
        voice, _tool_transfer_request(voice)
    )

    bridge._on_control(SimpleNamespace(data="stop"))

    assert bridge._queued_voice_tool_transfer is None
    assert statuses[-1] == (
        "voice-stopped",
        {
            "state": "cancelled",
            "success": False,
            "reason_code": "cancelled_by_runtime_control_before_dispatch",
        },
    )


def test_stop_and_reset_do_not_reopen_prior_command_id():
    bridge = _bare_bridge()
    assert bridge._dispatch_ledger.reserve("command-1", explicit_request_generation=3)

    bridge._on_control(SimpleNamespace(data="stop"))
    assert not bridge._dispatch_ledger.reserve("command-1", explicit_request_generation=3)

    bridge._on_control(SimpleNamespace(data="reset"))
    assert not bridge._dispatch_ledger.reserve("command-1", explicit_request_generation=3)


def test_only_start_or_start_actors_enable_external_dispatch():
    bridge = _bare_bridge()
    bridge._runtime_accepting_commands = False

    bridge._on_control(SimpleNamespace(data="start_runtime"))
    assert not bridge._runtime_accepting_commands
    assert bridge._route_initialization_state == "initializing"
    bridge._on_control(SimpleNamespace(data="start_actors"))
    assert bridge._runtime_accepting_commands
    assert bridge._route_initialization_state == "running"
    bridge._on_control(SimpleNamespace(data="stop"))
    assert not bridge._runtime_accepting_commands
    bridge._on_control(SimpleNamespace(data="start"))
    assert bridge._runtime_accepting_commands


def test_start_runtime_defers_first_bt_handover_until_start_actors():
    bridge = _bare_bridge()
    bridge._runtime_accepting_commands = False
    bridge._direct_hand_run_guard = lambda _command: ""
    bridge._public_instrument_identity = lambda _command: (
        "Adson forceps",
        "Adson forceps#1",
    )
    bridge._queue_voice_tool_transfer_preemption = lambda *_args: False
    bridge._publish_execution_route_state = lambda: None
    statuses: list[tuple[str, str]] = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        (command.command_id, kwargs["reason_code"])
    )
    dispatched: list[tuple[InternalSkillCommand, ToolHandoverRequest]] = []
    bridge._dispatch_tool_transfer = lambda command, request: dispatched.append(
        (command, request)
    )
    command = _skill("startup-bipolar-handover")
    message = SimpleNamespace(
        **{
            field: getattr(command, field)
            for field in command.__dataclass_fields__
        }
    )

    bridge._on_control(SimpleNamespace(data="start_runtime"))
    bridge._on_skill(message)

    assert dispatched == []
    assert statuses == [
        ("startup-bipolar-handover", "deferred_until_start_actors")
    ]
    assert bridge._deferred_startup_tool_transfer is not None

    bridge._on_control(SimpleNamespace(data="start_actors"))

    assert [item[0].command_id for item in dispatched] == [
        "startup-bipolar-handover"
    ]
    assert bridge._deferred_startup_tool_transfer is None
    assert bridge._runtime_accepting_commands


def test_stop_discards_bt_handover_deferred_during_startup_window():
    bridge = _bare_bridge()
    bridge._runtime_accepting_commands = False
    bridge._publish_execution_route_state = lambda: None
    command = _skill("startup-cancelled-handover")
    request = _tool_transfer_request(command)
    statuses: list[tuple[str, str]] = []
    bridge._publish_skill_status = lambda item, **kwargs: statuses.append(
        (item.command_id, kwargs["reason_code"])
    )

    bridge._on_control(SimpleNamespace(data="start_runtime"))
    assert bridge._defer_startup_tool_transfer(command, request)
    bridge._on_control(SimpleNamespace(data="stop"))

    assert bridge._deferred_startup_tool_transfer is None
    assert ("startup-cancelled-handover", "cancelled_before_start_actors") in statuses


def test_reset_is_repeatable_and_reopens_the_next_start_edge():
    bridge = _bare_bridge()
    bridge._runtime_accepting_commands = False
    bridge._last_lifecycle_control_signature = None
    bridge._bed_robot_revision = 7

    bridge._on_control(SimpleNamespace(data="start"))
    bridge._on_control(SimpleNamespace(data="start"))
    bridge._on_control(SimpleNamespace(data="reset"))
    assert bridge._bed_robot_revision is None
    bridge._bed_robot_revision = 9
    bridge._on_control(SimpleNamespace(data="reset"))
    assert bridge._bed_robot_revision is None
    bridge._on_control(SimpleNamespace(data="start"))

    assert bridge._runtime_accepting_commands
    assert bridge._last_lifecycle_control_signature == ("start", "")


def test_full_lifecycle_transport_duplicates_are_edge_idempotent():
    bridge = _bare_bridge()
    bridge._runtime_accepting_commands = False
    bridge._last_lifecycle_control_signature = None
    reset_calls: list[bool] = []
    bridge._dispatch_ledger = SimpleNamespace(
        clear=lambda: reset_calls.append(True)
    )

    for control in (
        "reset",
        "reset",
        "start_runtime:P03",
        "start_runtime:P03",
        "start_actors:P03",
        "start_actors:P03",
        "pause",
        "pause",
        "resume",
        "resume",
        "stop",
        "stop",
    ):
        bridge._on_control(SimpleNamespace(data=control))

    # ``stop`` is now also a clean-run boundary: it clears local command
    # dedupe records once, while its duplicated transport message remains
    # edge-idempotent.
    assert reset_calls == [True, True, True]
    assert bridge._runtime_accepting_commands is False
    assert bridge._last_lifecycle_control_signature == ("stop", "")


def _bed_robot_snapshot(
    revision: int = 1,
    *,
    state: str = "standby",
    direct_teach_active: bool = False,
    procedure_type: str = "nephrectomy",
    stamp_ns: int = 1_000_000_000,
) -> BedRobotArmStateArray:
    snapshot = BedRobotArmStateArray()
    snapshot.stamp.sec = stamp_ns // 1_000_000_000
    snapshot.stamp.nanosec = stamp_ns % 1_000_000_000
    snapshot.revision = revision
    snapshot.procedure_type = procedure_type
    if procedure_type == "thyroidectomy":
        layout = (("arm_1", "army_navy"),)
    elif procedure_type == "inguinal_hernia_repair":
        layout = (
            ("arm_1", "left_army_navy"),
            ("arm_2", "right_army_navy"),
        )
    else:
        layout = (
            ("arm_1", "left_malleable"),
            ("arm_2", "right_malleable"),
        )
    for arm_id, role_instance_id in layout:
        arm = BedRobotArmState()
        arm.arm_id = arm_id
        arm.role = "retraction"
        arm.role_instance_id = role_instance_id
        arm.state = state
        arm.direct_teach_active = direct_teach_active
        arm.reason_code = "ok"
        snapshot.arms.append(arm)
    return snapshot


def test_bed_robot_status_requires_monotonic_valid_controller_snapshots():
    bridge = _bare_bridge()
    bridge.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)

    bridge._on_bed_robot_status(_bed_robot_snapshot(2, stamp_ns=2_000_000_000))
    accepted_at = bridge._bed_robot_received_monotonic
    assert bridge._bed_robot_revision == 2
    assert set(bridge._bed_robot_states) == {"arm_1", "arm_2"}

    bridge._on_bed_robot_status(_bed_robot_snapshot(1, stamp_ns=1_000_000_000))
    assert bridge._bed_robot_revision == 2
    assert bridge._bed_robot_received_monotonic == accepted_at

    inconsistent = _bed_robot_snapshot(
        3,
        state="standby",
        direct_teach_active=True,
        stamp_ns=3_000_000_000,
    )
    bridge._on_bed_robot_status(inconsistent)
    assert bridge._bed_robot_revision == 2

    invalid_layout = _bed_robot_snapshot(3, stamp_ns=3_000_000_000)
    invalid_layout.procedure_type = "thyroidectomy"
    bridge._on_bed_robot_status(invalid_layout)
    assert bridge._bed_robot_revision == 2


def test_bed_robot_status_rejects_stale_and_future_source_time():
    bridge = _bare_bridge()
    bridge.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)
    bridge._bed_robot_source_max_age_sec = 2.0
    bridge._bed_robot_source_future_tolerance_sec = 0.5
    bridge._wall_time_ns = lambda: 10_000_000_000

    bridge._on_bed_robot_status(
        _bed_robot_snapshot(1, stamp_ns=7_000_000_000)
    )
    assert bridge._bed_robot_states == {}

    bridge._on_bed_robot_status(
        _bed_robot_snapshot(1, stamp_ns=11_000_000_000)
    )
    assert bridge._bed_robot_states == {}

    bridge._on_bed_robot_status(
        _bed_robot_snapshot(1, stamp_ns=9_000_000_000)
    )
    assert set(bridge._bed_robot_states) == {"arm_1", "arm_2"}


def test_dispatch_guard_rechecks_source_time_after_reception():
    bridge = _bare_bridge()
    snapshot = _bed_robot_snapshot()
    bridge._bed_robot_states = {arm.arm_id: arm for arm in snapshot.arms}
    bridge._bed_robot_procedure_type = "nephrectomy"
    bridge._bed_robot_received_monotonic = time.monotonic()
    bridge._bed_robot_source_max_age_sec = 2.0
    bridge._wall_time_ns = lambda: 10_000_000_000
    bridge._bed_robot_source_stamp_ns = 7_000_000_000
    request = _retraction_request("adjust-stale-source")

    assert (
        bridge._bed_robot_dispatch_guard(request)
        == "bed_robot_source_stamp_stale"
    )


def test_bed_robot_status_accepts_heartbeat_and_new_controller_epoch():
    bridge = _bare_bridge()
    bridge.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)

    bridge._on_bed_robot_status(_bed_robot_snapshot(9, stamp_ns=9_000_000_000))
    bridge._on_bed_robot_status(_bed_robot_snapshot(9, stamp_ns=10_000_000_000))
    assert bridge._bed_robot_revision == 9
    assert bridge._bed_robot_source_stamp_ns == 10_000_000_000
    assert bridge._bed_robot_epoch == 0

    bridge._on_bed_robot_status(_bed_robot_snapshot(1, stamp_ns=11_000_000_000))
    assert bridge._bed_robot_revision == 1
    assert bridge._bed_robot_source_stamp_ns == 11_000_000_000
    assert bridge._bed_robot_epoch == 1


def test_controller_restart_during_command_preserves_tracking_and_blocks_dispatch():
    bridge = _bare_bridge()
    bridge.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)
    command = _group("adjust-during-restart")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction",
        command=command,
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_bed_robot_status(_bed_robot_snapshot(8, stamp_ns=8_000_000_000))
    bridge._on_bed_robot_status(_bed_robot_snapshot(1, stamp_ns=9_000_000_000))

    assert ("retraction", command.command_id) in bridge._active_services
    assert not bridge._runtime_is_accepting()
    assert statuses == [{
        "state": "unknown",
        "outcome": "remote_state_unknown",
        "terminal": False,
        "success": False,
        "reason_code": "controller_restarted_during_command",
    }]


def test_bed_robot_status_rejects_changed_payload_without_revision_advance():
    bridge = _bare_bridge()
    bridge.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)

    bridge._on_bed_robot_status(_bed_robot_snapshot(4, stamp_ns=4_000_000_000))
    bridge._on_bed_robot_status(
        _bed_robot_snapshot(
            4,
            state="retracting",
            stamp_ns=5_000_000_000,
        )
    )

    assert bridge._bed_robot_revision == 4
    assert bridge._bed_robot_source_stamp_ns == 4_000_000_000
    assert all(arm.state == "standby" for arm in bridge._bed_robot_states.values())


def test_dispatch_guard_fails_closed_on_missing_stale_and_direct_teach_status():
    bridge = _bare_bridge()
    request = _retraction_request()
    assert bridge._bed_robot_dispatch_guard(request) == "bed_robot_status_missing"

    bridge._bed_robot_states = {"arm_1": _bed_robot_snapshot().arms[0]}
    bridge._bed_robot_procedure_type = "nephrectomy"
    bridge._bed_robot_received_monotonic = time.monotonic() - 3.0
    assert bridge._bed_robot_dispatch_guard(request) == "bed_robot_status_stale"

    direct_teach = _bed_robot_snapshot(
        state="direct_teach", direct_teach_active=True
    ).arms[0]
    bridge._bed_robot_states = {"arm_1": direct_teach}
    bridge._bed_robot_procedure_type = "nephrectomy"
    bridge._bed_robot_received_monotonic = time.monotonic()
    bridge._bed_robot_source_stamp_ns = time.time_ns()
    assert bridge._bed_robot_dispatch_guard(request) == "direct_teach_active"


def test_dispatch_guard_accepts_only_a_fresh_standby_target():
    bridge = _bare_bridge()
    snapshot = _bed_robot_snapshot()
    bridge._bed_robot_states = {arm.arm_id: arm for arm in snapshot.arms}
    bridge._bed_robot_procedure_type = "nephrectomy"
    bridge._bed_robot_received_monotonic = time.monotonic()
    bridge._bed_robot_source_stamp_ns = time.time_ns()
    request = _retraction_request()
    assert bridge._bed_robot_dispatch_guard(request) == ""


def test_inguinal_dispatch_guard_targets_the_matching_army_navy_side() -> None:
    bridge = _bare_bridge()
    snapshot = _bed_robot_snapshot(procedure_type="inguinal_hernia_repair")
    bridge._bed_robot_states = {arm.arm_id: arm for arm in snapshot.arms}
    bridge._bed_robot_procedure_type = "inguinal_hernia_repair"
    bridge._bed_robot_received_monotonic = time.monotonic()
    bridge._bed_robot_source_stamp_ns = time.time_ns()

    assert bridge._bed_robot_dispatch_guard(_retraction_request()) == ""
    assert bridge._bed_robot_dispatch_guard(
        _retraction_request(
            "inguinal-right",
            target_side=RETRACTION_TARGET_RIGHT,
        )
    ) == ""


def test_retraction_adjustment_accepts_controller_retracting_state():
    bridge = _bare_bridge()
    snapshot = _bed_robot_snapshot(state="retracting")
    bridge._bed_robot_states = {arm.arm_id: arm for arm in snapshot.arms}
    bridge._bed_robot_procedure_type = "nephrectomy"
    bridge._bed_robot_received_monotonic = time.monotonic()
    bridge._bed_robot_source_stamp_ns = time.time_ns()
    request = _retraction_request(
        "adjust-active",
        target_side=RETRACTION_TARGET_RIGHT,
        distance_m=0.003,
    )

    assert bridge._bed_robot_dispatch_guard(request) == ""


def test_tool_change_only_requires_fresh_generic_controller_status():
    bridge = _bare_bridge()
    snapshot = _bed_robot_snapshot(procedure_type="thyroidectomy")
    bridge._bed_robot_states = {arm.arm_id: arm for arm in snapshot.arms}
    bridge._bed_robot_procedure_type = "thyroidectomy"
    bridge._bed_robot_received_monotonic = time.monotonic()
    bridge._bed_robot_source_stamp_ns = time.time_ns()
    request = _retraction_request(
        "change-1",
        command=RETRACTION_COMMAND_CHANGE_TOOL,
        target_side=RETRACTION_TARGET_NONE,
        distance_m=0.0,
    )

    assert bridge._bed_robot_dispatch_guard(request) == ""
    bridge._bed_robot_states["arm_1"].state = "retracting"
    assert bridge._bed_robot_dispatch_guard(request) == ""


def test_completed_handover_event_contains_only_the_dt_reconciliation_fields():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)

    bridge._publish_tool_transfer_completed_events(
        _skill(),
        final_state="completed",
        reason_code="completed",
        semantic_leg=("tray", "surgeon"),
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "ToolHandoverCompleted"
    assert event.instrument_id == "T04"
    assert event.instance_id == "T04#1"
    assert event.source_location_type == "tray_slot"
    assert event.source_location_id == "tray-a-2"
    assert event.target_location_type == "handover_zone"
    assert event.target_location_id == "surgeon_receive_zone"
    assert event.arm == ""
    assert json.loads(event.detail_json) == {
        "command_id": "skill-1",
        "authoritative_controller_completion": True,
        "controller_final_state": "completed",
        "controller_reason_code": "completed",
        "controller_source_location": "tray",
        "controller_target_location": "surgeon",
        "controller_projection_step": "handover_completed",
        "controller_projection_index": 1,
        "controller_projection_count": 1,
        "request_generation": 4,
        "voice_backed": False,
    }
    assert event.target_owner == ""
    assert not event.cleaning_required
    assert event.mode == ""


def test_completed_retrieve_reconciles_pickup_then_return_to_tray():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    command = replace(
        _skill(),
        action="retrieve_from_mayo",
        source_location_type="mayo_recovery_zone",
        source_location_id="mayo_recovery_zone",
        target_location_type="tray_slot",
        target_location_id="tray-a-2",
    )

    bridge._publish_tool_transfer_completed_events(
        command,
        final_state="completed",
        reason_code="completed",
        semantic_leg=("mayo", "tray"),
    )

    assert [event.event_type for event in events] == [
        "ToolRetrievedFromMayo",
        "ToolReturnedToTray",
    ]
    assert events[0].source_location_id == "mayo_recovery_zone"
    assert events[0].target_location_id == "robot_left_hand"
    assert events[1].source_location_id == "robot_left_hand"
    assert events[1].target_location_id == "tray-a-2"
    assert all(
        json.loads(event.detail_json)["authoritative_controller_completion"]
        for event in events
    )
    assert [
        json.loads(event.detail_json)["controller_projection_step"]
        for event in events
    ] == ["retrieved_from_mayo", "returned_to_tray"]


def test_completed_robot_to_tray_recovery_projects_one_authoritative_step():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    command = replace(
        _skill(),
        source_location_type="robot_right_hand",
        source_location_id="robot_right_hand",
        target_location_type="tray_slot",
        target_location_id="tray-a-2",
    )

    bridge._publish_tool_transfer_completed_events(
        command,
        final_state="completed",
        reason_code="completed",
        semantic_leg=("robot", "tray"),
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "ToolReturnedToTray"
    assert event.source_location_type == "robot"
    assert event.target_location_type == "tray_slot"
    assert json.loads(event.detail_json) == {
        "authoritative_controller_completion": True,
        "command_id": "skill-1",
        "controller_final_state": "completed",
        "controller_projection_count": 1,
        "controller_projection_index": 1,
        "controller_projection_step": "returned_to_tray",
        "controller_reason_code": "completed",
        "controller_source_location": "robot",
        "controller_target_location": "tray",
        "request_generation": 4,
        "voice_backed": False,
    }


def test_completed_prepare_records_a_generic_robot_hold_without_an_arm():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    command = replace(
        _skill(),
        action="predict_tool",
        target_location_type="robot_right_hand",
        target_location_id="robot_right_hand",
        mode="anticipatory",
    )

    bridge._publish_tool_prepared_event(
        command,
        final_state="completed",
        reason_code="completed",
        semantic_leg=("tray", "robot"),
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "ToolPrepared"
    assert event.status == "prepared"
    assert event.arm == ""
    assert event.source_location_type == "tray_slot"
    assert event.source_location_id == "tray-a-2"
    assert event.target_location_type == "robot"
    assert event.target_location_id == "robot"
    assert event.target_owner == ""
    assert event.mode == ""


def test_completed_mayo_prepare_preserves_mayo_origin_for_the_digital_twin():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    command = replace(
        _skill(),
        action="predict_tool",
        source_location_type="mayo_reuse_zone",
        source_location_id="mayo_reuse_zone",
        target_location_type="robot_right_hand",
        target_location_id="robot_right_hand",
        mode="anticipatory",
    )

    bridge._publish_tool_transfer_completed_events(
        command,
        final_state="completed",
        reason_code="completed",
        semantic_leg=("mayo", "robot"),
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "ToolPrepared"
    assert event.source_location_type == "mayo_reuse_zone"
    assert event.source_location_id == "mayo_reuse_zone"
    assert event.target_location_type == "robot"
    assert event.target_location_id == "robot"


def test_unused_preposition_return_ignores_tray_hint_and_parks_on_mayo():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    command = replace(
        _skill(),
        action="return_unused_preposition",
        source_location_type="robot_right_hand",
        source_location_id="robot_right_hand",
        target_location_type="tray_slot",
        target_location_id="main_tray_slot_4",
    )

    bridge._publish_tool_transfer_completed_events(
        command,
        final_state="completed",
        reason_code="completed",
        semantic_leg=("robot", "mayo"),
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "UnusedPrepositionReturned"
    assert event.arm == ""
    assert event.source_location_type == "robot"
    assert event.source_location_id == "robot"
    assert event.target_location_type == "mayo_stand"
    assert event.target_location_id == "mayo_stand"
    detail = json.loads(event.detail_json)
    assert detail["authoritative_controller_completion"] is True
    assert detail["controller_source_location"] == "robot"
    assert detail["controller_target_location"] == "mayo"


def test_unused_mayo_preposition_return_is_canonicalized_to_mayo_stand():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    command = replace(
        _skill(),
        action="return_unused_preposition",
        source_location_type="robot_right_hand",
        source_location_id="robot_right_hand",
        target_location_type="mayo_reuse_zone",
        target_location_id="mayo_reuse_zone",
    )

    bridge._publish_tool_transfer_completed_events(
        command,
        final_state="completed",
        reason_code="completed",
        semantic_leg=("robot", "mayo"),
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "UnusedPrepositionReturned"
    assert event.source_location_type == "robot"
    assert event.source_location_id == "robot"
    assert event.target_location_type == "mayo_stand"
    assert event.target_location_id == "mayo_stand"


def test_failed_never_publishes_completion_and_cancel_only_reconciles_inventory():
    bridge = _bare_bridge()
    events = []
    cancel_events = []
    statuses = []
    bridge._publish_tool_transfer_completed_events = (
        lambda *args, **kwargs: events.append((args, kwargs))
    )
    bridge._publish_tool_transfer_cancel_reconciliation = (
        lambda *args, **kwargs: cancel_events.append((args, kwargs))
    )
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)
    failed_command = _skill()
    _activate_tool_transfer(bridge, failed_command)
    failed_future = SimpleNamespace(
        result=lambda: SimpleNamespace(
            status=GoalStatus.STATUS_ABORTED,
            result=SimpleNamespace(
                success=False,
                final_state="failed",
                reason_code="controller_rejected",
            )
        )
    )

    bridge._on_tool_transfer_result(failed_command, failed_future)
    assert events == []
    assert cancel_events == []
    assert statuses[-1]["reason_code"] == "controller_rejected"

    canceled_command = _skill("cancelled-1")
    _activate_tool_transfer(bridge, canceled_command, cancel_requested=True)
    canceled_future = SimpleNamespace(
        result=lambda: SimpleNamespace(
            status=GoalStatus.STATUS_CANCELED,
            result=SimpleNamespace(
                success=False,
                final_state="canceled",
                reason_code="canceled_recovered_to_tray",
            )
        )
    )
    bridge._on_tool_transfer_result(canceled_command, canceled_future)
    assert events == []
    assert len(cancel_events) == 1
    assert cancel_events[0][1] == {
        "final_state": "canceled",
        "reason_code": "canceled_recovered_to_tray",
    }
    assert statuses[-1] == {
        "state": "canceled",
        "success": False,
        "reason_code": "canceled_recovered_to_tray",
        "progress": 1.0,
    }
    assert (
        bridge._begin_action_dispatch(
            "tool_transfer",
            replace(_skill("after-recovery"), request_generation=6),
        )
        == ""
    )


def test_retraction_service_response_must_match_admission_contract():
    bridge = _bare_bridge()
    command = _group("adjust-status-mismatch")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction", command=command
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_retraction_service_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                request_accepted=True,
                result_code=ExecuteRetractionCommand.Response.RESULT_REJECTED,
                command_id=command.command_id,
                message="inconsistent",
            )
        ),
    )

    assert ("retraction", command.command_id) in bridge._active_services
    assert not bridge._runtime_is_accepting()
    assert statuses[-1] == {
        "state": "unknown",
        "outcome": "remote_state_unknown",
        "terminal": False,
        "success": False,
        "reason_code": "invalid_service_response",
    }


def test_action_terminal_status_must_match_tool_transfer_payload():
    bridge = _bare_bridge()
    command = _skill("handover-status-mismatch")
    _activate_tool_transfer(bridge, command)
    events = []
    statuses = []
    bridge._publish_tool_transfer_completed_events = (
        lambda *args, **kwargs: events.append((args, kwargs))
    )
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_tool_transfer_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                status=GoalStatus.STATUS_ABORTED,
                result=SimpleNamespace(
                    success=True,
                    final_state="completed",
                    reason_code="completed",
                ),
            )
        ),
    )

    assert events == []
    assert ("tool_transfer", command.command_id) not in bridge._active_actions
    assert not bridge._runtime_is_accepting()
    assert statuses[-1] == {
        "state": "failed",
        "success": False,
        "reason_code": "invalid_controller_result",
        "progress": 1.0,
    }


def test_failed_tray_to_robot_transfer_never_publishes_a_prepared_event():
    bridge = _bare_bridge()
    events = []
    statuses = []
    bridge._publish_tool_transfer_completed_events = (
        lambda *args, **kwargs: events.append((args, kwargs))
    )
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)
    command = replace(_skill(), action="predict_tool")
    _activate_tool_transfer(bridge, command)
    failed_future = SimpleNamespace(
        result=lambda: SimpleNamespace(
            status=GoalStatus.STATUS_ABORTED,
            result=SimpleNamespace(
                success=False,
                final_state="failed",
                reason_code="grasp_failed",
            )
        )
    )

    bridge._on_tool_transfer_result(command, failed_future)

    assert events == []
    assert statuses[-1]["reason_code"] == "grasp_failed"


def test_inconsistent_success_result_fails_closed_without_a_dt_event():
    bridge = _bare_bridge()
    events = []
    statuses = []
    bridge._publish_tool_transfer_completed_events = (
        lambda *args, **kwargs: events.append((args, kwargs))
    )
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)
    command = _skill()
    _activate_tool_transfer(bridge, command)
    inconsistent_future = SimpleNamespace(
        result=lambda: SimpleNamespace(
            status=GoalStatus.STATUS_ABORTED,
            result=SimpleNamespace(
                success=True,
                final_state="failed",
                reason_code="vendor_specific_success",
            )
        )
    )

    bridge._on_tool_transfer_result(command, inconsistent_future)

    assert events == []
    assert statuses[-1] == {
        "state": "failed",
        "success": False,
        "reason_code": "invalid_controller_result",
        "progress": 1.0,
    }
def test_canceled_result_requires_a_machine_readable_recovery_outcome():
    bridge = _bare_bridge()
    events = []
    statuses = []
    command = _skill()
    _activate_tool_transfer(bridge, command, cancel_requested=True)
    bridge._publish_tool_transfer_completed_events = (
        lambda *args, **kwargs: events.append((args, kwargs))
    )
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)
    ambiguous_future = SimpleNamespace(
        result=lambda: SimpleNamespace(
            status=GoalStatus.STATUS_CANCELED,
            result=SimpleNamespace(
                success=False,
                final_state="canceled",
                reason_code="operator_cancel",
            )
        )
    )

    bridge._on_tool_transfer_result(command, ambiguous_future)

    assert events == []
    assert statuses[-1] == {
        "state": "failed",
        "success": False,
        "reason_code": "invalid_controller_result",
        "progress": 1.0,
    }
    assert not bridge._runtime_is_accepting()
