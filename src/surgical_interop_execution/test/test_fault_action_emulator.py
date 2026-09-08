import json
from pathlib import Path
import threading
from types import SimpleNamespace
import time

import pytest
from rclpy.action import CancelResponse, GoalResponse

from procedure_spec import compute_bundle_config_revision, scenario_config_payload
from procedure_spec.scenario_consumer import ScenarioConfigConsumerBinding
from surgical_interop_execution.fault_action_emulator import (
    _BED_ROBOT_STATUS_PERIOD_SEC,
    EmulatorProfile,
    FaultActionEmulator,
    Outcome,
    RouteProfile,
    validate_retraction_command,
    valid_tool_transition,
)
from surgical_interop_execution.controller_contract import (
    EIR_NUC_CAPABILITY_POLICY_ID,
    VIRTUAL_EMULATOR_CAPABILITY_POLICY_ID,
    validate_tool_handover_fields,
)
from surgical_interop_msgs.srv import ExecuteRetractionCommand


def test_only_reviewed_tool_transitions_are_accepted():
    assert valid_tool_transition("tray", "robot")
    assert valid_tool_transition("tray", "surgeon")
    assert valid_tool_transition("robot", "surgeon")
    assert valid_tool_transition("robot", "tray")
    assert valid_tool_transition("robot", "mayo")
    assert valid_tool_transition("mayo", "robot")
    assert valid_tool_transition("mayo", "tray")
    assert not valid_tool_transition("surgeon", "robot")
    assert not valid_tool_transition("mayo", "surgeon")


def test_eir_capability_policy_rejects_unknown_tool_before_action_execution():
    assert validate_tool_handover_fields(
        instrument_id="Bovie surgical cautery",
        instrument_instance_id="Bovie surgical cautery#1",
        source_location="tray",
        target_location="surgeon",
        capability_policy_id=EIR_NUC_CAPABILITY_POLICY_ID,
    ) == ""
    assert validate_tool_handover_fields(
        instrument_id="Allis clamp forceps",
        instrument_instance_id="Allis clamp forceps#1",
        source_location="tray",
        target_location="surgeon",
        capability_policy_id=EIR_NUC_CAPABILITY_POLICY_ID,
    ) == "instrument_not_supported_by_capability_policy"
    assert validate_tool_handover_fields(
        instrument_id="",
        instrument_instance_id="",
        source_location="tray",
        target_location="surgeon",
        capability_policy_id=EIR_NUC_CAPABILITY_POLICY_ID,
    ) == "missing_instrument_identity"


def test_profile_sequence_is_deterministic(tmp_path: Path):
    path = tmp_path / "profile.yaml"
    path.write_text(
        """schema: taskplanner.action_emulator.v1
profile_id: test
routes:
  tool_handover:
    sequence:
      - {outcome: partial_failure, duration_sec: 0.1, fail_progress: 0.4}
      - {outcome: success, duration_sec: 0.2}
    default: {outcome: abort, reason_code: exhausted}
  retraction_command: {available: false}
""",
        encoding="utf-8",
    )
    profile = EmulatorProfile.load(path)
    route = profile.routes["tool_handover"]
    assert route.next().outcome == "partial_failure"
    assert route.next().outcome == "success"
    assert route.next().reason_code == "exhausted"
    assert profile.routes["retraction_command"].available is False
    assert "suction" not in profile.routes


def test_unknown_outcome_is_rejected(tmp_path: Path):
    path = tmp_path / "profile.yaml"
    path.write_text(
        """schema: taskplanner.action_emulator.v1
routes:
  tool_handover: {default: {outcome: teleport}}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported emulator outcome"):
        EmulatorProfile.load(path)


def _command(**overrides):
    values = {
        "protocol_version": 1,
        "source_id": "taskplanner",
        "command_id": "adjust-1",
        "command": ExecuteRetractionCommand.Request.COMMAND_ADJUST_RETRACTION,
        "target_side": ExecuteRetractionCommand.Request.TARGET_LEFT,
        "distance_m": 0.005,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("overrides", "result_code", "reason"),
    [
        (
            {"protocol_version": 99},
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            "unsupported_protocol_version",
        ),
        (
            {"source_id": ""},
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            "missing_source_id",
        ),
        (
            {"command_id": ""},
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            "missing_command_id",
        ),
        (
            {
                "command": 99,
            },
            ExecuteRetractionCommand.Response.RESULT_INVALID_COMMAND,
            "invalid_command",
        ),
        (
            {"distance_m": 0.0},
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            "invalid_adjust_distance_m",
        ),
        (
            {"distance_m": 0.051},
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            "invalid_adjust_distance_m",
        ),
        (
            {"distance_m": -0.051},
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            "invalid_adjust_distance_m",
        ),
    ],
)
def test_retraction_command_contract_rejects_invalid_request_fields(
    overrides, result_code, reason
):
    assert validate_retraction_command(_command(**overrides)) == (result_code, reason)


def test_retraction_command_contract_accepts_adjust_and_preconfigured_tool_change():
    assert validate_retraction_command(_command()) == (
        ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
        "",
    )
    assert validate_retraction_command(
        _command(
            command=ExecuteRetractionCommand.Request.COMMAND_CHANGE_TOOL,
            target_side=ExecuteRetractionCommand.Request.TARGET_NONE,
            distance_m=0.0,
        )
    ) == (
        ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
        "",
    )


def test_retraction_command_contract_accepts_zero_argument_suction() -> None:
    assert validate_retraction_command(
        _command(
            command=ExecuteRetractionCommand.Request.COMMAND_SUCTION,
            target_side=ExecuteRetractionCommand.Request.TARGET_NONE,
            distance_m=0.0,
        )
    ) == (ExecuteRetractionCommand.Response.RESULT_ACCEPTED, "")


def test_retraction_command_contract_accepts_zero_argument_suction_out() -> None:
    assert validate_retraction_command(
        _command(
            command=ExecuteRetractionCommand.Request.COMMAND_SUCTION_OUT,
            target_side=ExecuteRetractionCommand.Request.TARGET_NONE,
            distance_m=0.0,
        )
    ) == (ExecuteRetractionCommand.Response.RESULT_ACCEPTED, "")


def test_retraction_command_contract_accepts_negative_adjustment() -> None:
    assert validate_retraction_command(
        _command(
            command=ExecuteRetractionCommand.Request.COMMAND_ADJUST_RETRACTION,
            target_side=ExecuteRetractionCommand.Request.TARGET_RIGHT,
            distance_m=-0.001,
        )
    ) == (ExecuteRetractionCommand.Response.RESULT_ACCEPTED, "")


@pytest.mark.parametrize(
    "target_side",
    [
        ExecuteRetractionCommand.Request.TARGET_NONE,
        ExecuteRetractionCommand.Request.TARGET_LEFT,
        ExecuteRetractionCommand.Request.TARGET_RIGHT,
    ],
)
def test_retraction_command_contract_accepts_finish_with_optional_target_side(
    target_side,
):
    assert validate_retraction_command(
        _command(
            command=ExecuteRetractionCommand.Request.COMMAND_FINISH_DIRECT_TEACH,
            target_side=target_side,
            distance_m=0.0,
        )
    ) == (ExecuteRetractionCommand.Response.RESULT_ACCEPTED, "")


def test_retraction_command_contract_accepts_bilateral_adjustment() -> None:
    assert validate_retraction_command(
        _command(
            target_side=ExecuteRetractionCommand.Request.TARGET_BOTH,
            distance_m=0.001,
        )
    ) == (ExecuteRetractionCommand.Response.RESULT_ACCEPTED, "")


def test_retraction_command_contract_rejects_none_adjustment_target() -> None:
    assert validate_retraction_command(
        _command(
            target_side=ExecuteRetractionCommand.Request.TARGET_NONE,
            distance_m=0.001,
        )
    ) == (
        ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
        "adjust_requires_left_right_or_both_target",
    )


def test_retraction_command_contract_rejects_both_finish_target() -> None:
    assert validate_retraction_command(
        _command(
            command=ExecuteRetractionCommand.Request.COMMAND_FINISH_DIRECT_TEACH,
            target_side=ExecuteRetractionCommand.Request.TARGET_BOTH,
            distance_m=0.0,
        )
    ) == (
        ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
        "command_does_not_accept_target_or_distance",
    )


def _bare_emulator(outcome: Outcome) -> FaultActionEmulator:
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    emulator._lock = __import__("threading").RLock()
    emulator._active_ids = set()
    emulator._selected_outcomes = {}
    emulator._completed = {}
    emulator._route_counts = {}
    emulator._max_retraction_distance_m = 0.050
    emulator._profile = SimpleNamespace(
        routes={
            "retraction_command": RouteProfile(default=outcome),
        }
    )
    return emulator


def _tool_action_emulator() -> FaultActionEmulator:
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    emulator._lock = threading.RLock()
    emulator._active_ids = set()
    emulator._selected_outcomes = {}
    emulator._completed = {}
    emulator._route_counts = {}
    emulator._capability_policy_id = VIRTUAL_EMULATOR_CAPABILITY_POLICY_ID
    emulator._profile = SimpleNamespace(
        routes={
            "tool_handover": RouteProfile(
                default=Outcome(outcome="success", duration_sec=0.0)
            ),
        }
    )
    return emulator


def _tool_goal(command_id: str):
    return SimpleNamespace(
        command_id=command_id,
        instrument_id="Adson forceps",
        instrument_instance_id="Adson forceps#1",
        source_location="tray",
        target_location="surgeon",
    )


def test_emulator_rejects_every_goal_until_active_action_is_terminal() -> None:
    emulator = _tool_action_emulator()
    first = _tool_goal("first")

    assert emulator._goal("tool_handover", first) == GoalResponse.ACCEPT
    assert emulator._goal("tool_handover", first) == GoalResponse.REJECT
    assert emulator._goal("tool_handover", _tool_goal("second")) == (
        GoalResponse.REJECT
    )
    assert emulator._cancel(SimpleNamespace(request=first)) == CancelResponse.ACCEPT
    assert emulator._active_ids == {"first"}
    assert emulator._route_counts["tool_handover"] == {
        "rejected_duplicate_active": 1,
        "rejected_action_inflight": 1,
    }

    emulator._finish("tool_handover", "first", "canceled", "cancel_recovered")

    assert emulator._goal("tool_handover", _tool_goal("after-terminal")) == (
        GoalResponse.ACCEPT
    )


def test_emulator_goal_reservation_is_atomic_under_race() -> None:
    emulator = _tool_action_emulator()
    barrier = threading.Barrier(3)
    responses = []
    response_lock = threading.Lock()

    def submit(command_id: str) -> None:
        barrier.wait()
        response = emulator._goal("tool_handover", _tool_goal(command_id))
        with response_lock:
            responses.append(response)

    threads = [
        threading.Thread(target=submit, args=("race-a",)),
        threading.Thread(target=submit, args=("race-b",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2.0)

    assert not any(thread.is_alive() for thread in threads)
    assert responses.count(GoalResponse.ACCEPT) == 1
    assert responses.count(GoalResponse.REJECT) == 1
    assert len(emulator._active_ids) == 1
    assert emulator._route_counts["tool_handover"] == {
        "rejected_action_inflight": 1,
    }


def _scenario_observer_emulator(
    *,
    retraction_available: bool = True,
    scenario_config_root: Path | None = None,
):
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    emulator._lock = __import__("threading").RLock()
    emulator._active_ids = set()
    emulator._selected_outcomes = {}
    emulator._completed = {}
    emulator._route_counts = {}
    emulator._profile = SimpleNamespace(
        profile_id="scenario-observer",
        routes={
            "tool_handover": RouteProfile(available=True),
            "retraction_command": RouteProfile(available=retraction_available),
        },
    )
    emulator._publish_bed_robot_status_enabled = True
    emulator._procedure_type = "nephrectomy"
    scenario_config_root = scenario_config_root or (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    emulator._scenario_config = ScenarioConfigConsumerBinding(
        fixed_spec_root=scenario_config_root
    )
    emulator._scenario_state_received = False
    emulator._scenario_running = False
    emulator._scenario_execution_state = ""
    emulator._scenario_initial_idle = True
    warnings = []
    emulator.get_logger = lambda: SimpleNamespace(
        warning=warnings.append,
        info=lambda _message: None,
    )
    return emulator, warnings


def _scenario_config_message(spec_dir: Path, *, revision: str | None = None):
    resolved = spec_dir.resolve()
    return SimpleNamespace(
        data=json.dumps(
            scenario_config_payload(
                bundle_name=resolved.name,
                spec_dir=str(resolved),
                revision=revision or compute_bundle_config_revision(resolved),
            )
        )
    )


def test_emulator_projects_selected_procedure_type_at_initial_idle():
    emulator, warnings = _scenario_observer_emulator()
    candidate = emulator._scenario_config.fixed_spec_root / "thyroidectomy_demo"

    emulator._on_scenario_config(_scenario_config_message(candidate))

    assert emulator._procedure_type == "thyroidectomy"
    assert emulator._scenario_config.revision == compute_bundle_config_revision(candidate)
    assert emulator._scenario_config.pending_snapshot() is None
    assert warnings == []


def test_emulator_defers_projection_until_no_request_is_inflight():
    emulator, _warnings = _scenario_observer_emulator()
    candidate = emulator._scenario_config.fixed_spec_root / "thyroidectomy_demo"
    emulator._active_ids.add("tool-1")

    emulator._on_scenario_config(_scenario_config_message(candidate))

    assert emulator._procedure_type == "nephrectomy"
    assert emulator._scenario_config.pending_snapshot() is not None

    emulator._active_ids.clear()
    emulator._on_simulation_state(
        SimpleNamespace(running=False, execution_state="stopped")
    )

    assert emulator._procedure_type == "thyroidectomy"
    assert emulator._scenario_config.pending_snapshot() is None


def test_emulator_rejects_authored_edit_while_waiting_for_quiescence(tmp_path):
    source_root = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    copied_root = tmp_path / "specs"
    import shutil

    shutil.copytree(source_root, copied_root)
    emulator, warnings = _scenario_observer_emulator(
        scenario_config_root=copied_root
    )
    candidate = copied_root / "thyroidectomy_demo"
    emulator._active_ids.add("in-flight")

    emulator._on_scenario_config(_scenario_config_message(candidate))
    assert emulator._scenario_config.pending_snapshot() is not None

    prompt = candidate / "vlm_procedure_prompt.yaml"
    prompt.write_text(
        prompt.read_text(encoding="utf-8") + "\n# edited while busy\n",
        encoding="utf-8",
    )
    emulator._active_ids.clear()
    emulator._on_simulation_state(
        SimpleNamespace(running=False, execution_state="stopped")
    )

    assert emulator._procedure_type == "nephrectomy"
    assert emulator._scenario_config.revision == ""
    assert emulator._scenario_config.pending_snapshot() is None
    assert warnings and "revision does not match" in warnings[-1]


def test_emulator_applies_projection_when_selected_bundle_needs_unhosted_route():
    emulator, _warnings = _scenario_observer_emulator(retraction_available=False)
    candidate = emulator._scenario_config.fixed_spec_root / "thyroidectomy_demo"

    emulator._on_scenario_config(_scenario_config_message(candidate))

    # Endpoint wiring is process-lifetime, but an emulator need not advertise
    # a topology restart just because a selected scenario would normally use
    # an endpoint it does not host.  Its local spec projection still converges
    # at the stopped/idle boundary.
    assert emulator._procedure_type == "thyroidectomy"
    assert emulator._scenario_config.pending_snapshot() is None


def test_emulator_replaces_projection_without_restart_hint():
    emulator, _warnings = _scenario_observer_emulator(retraction_available=False)
    root = emulator._scenario_config.fixed_spec_root

    emulator._on_scenario_config(_scenario_config_message(root / "thyroidectomy_demo"))
    assert emulator._procedure_type == "thyroidectomy"

    emulator._on_scenario_config(
        _scenario_config_message(
            root / "inguinal_hernia_repair",
            revision=compute_bundle_config_revision(root / "inguinal_hernia_repair"),
        )
    )

    # This bundle does not request a procedure-specific bed-arm projection.
    assert emulator._procedure_type == ""
    assert emulator._scenario_config.revision == compute_bundle_config_revision(
        root / "inguinal_hernia_repair"
    )
    assert emulator._scenario_config.pending_snapshot() is None


def test_emulator_status_has_no_restart_required_topology_field():
    emulator, _warnings = _scenario_observer_emulator(retraction_available=False)
    candidate = emulator._scenario_config.fixed_spec_root / "thyroidectomy_demo"
    emulator._on_scenario_config(_scenario_config_message(candidate))
    published = []
    emulator._status_pub = SimpleNamespace(publish=published.append)
    emulator._robot_endpoint_source = "virtual"
    emulator._controller_contract_id = "test-contract"
    emulator._controller_contract_pub = None
    emulator._capability_policy_id = "test-policy"

    emulator._publish_status()

    payload = json.loads(published[-1].data)
    assert payload["scenario_config_pending_bundle"] == ""
    assert "scenario_config_restart_required" not in payload
    assert "scenario_config_restart_reason" not in payload


@pytest.mark.parametrize("parameter_name", ("scenario_config_spec_root", "scenario_config_topic"))
def test_emulator_rejects_runtime_rebind_of_scenario_observer(parameter_name):
    result = FaultActionEmulator._on_endpoint_configuration_parameters_changed(
        [SimpleNamespace(name=parameter_name, value="ignored")]
    )

    assert result.successful is False
    assert "launch-lifetime" in result.reason


def test_bed_robot_status_heartbeat_publishes_initial_snapshot_before_timer():
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    calls = []
    timer = object()
    emulator._publish_bed_robot_status = lambda: calls.append(("publish", None))

    def create_timer(period_sec, callback):
        calls.append(("timer", period_sec, callback))
        return timer

    emulator.create_timer = create_timer

    emulator._start_bed_robot_status_heartbeat()

    assert calls[0] == ("publish", None)
    assert calls[1][0:2] == ("timer", _BED_ROBOT_STATUS_PERIOD_SEC)
    assert calls[1][2] is emulator._publish_bed_robot_status
    assert _BED_ROBOT_STATUS_PERIOD_SEC == 0.5
    assert emulator._bed_robot_status_timer is timer


def test_bed_robot_status_revisions_are_monotonic_across_checkpoints():
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    published = []
    stamps = iter((11, 12, 13))
    emulator._bed_robot_revision = 0
    emulator._procedure_type = "thyroidectomy"
    emulator._bed_robot_status_pub = SimpleNamespace(
        publish=lambda message: published.append(message)
    )
    emulator.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(
            to_msg=lambda: SimpleNamespace(sec=next(stamps), nanosec=0)
        )
    )

    emulator._publish_bed_robot_status()
    emulator._publish_bed_robot_status()

    assert [message.revision for message in published] == [1, 2]
    assert [message.stamp.sec for message in published] == [11, 12]
    assert all(message.procedure_type == "thyroidectomy" for message in published)
    assert all(len(message.arms) == 1 for message in published)
    assert all(message.arms[0].state == "standby" for message in published)


def test_inguinal_status_uses_two_army_navy_retraction_roles():
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    published = []
    emulator._bed_robot_revision = 0
    emulator._procedure_type = "inguinal_hernia_repair"
    emulator._bed_robot_status_pub = SimpleNamespace(publish=published.append)
    emulator.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(
            to_msg=lambda: SimpleNamespace(sec=11, nanosec=0)
        )
    )

    emulator._publish_bed_robot_status()

    assert len(published) == 1
    assert published[0].procedure_type == "inguinal_hernia_repair"
    assert {
        (arm.arm_id, arm.role_instance_id)
        for arm in published[0].arms
    } == {
        ("arm_1", "left_army_navy"),
        ("arm_2", "right_army_navy"),
    }


@pytest.mark.parametrize(
    ("topic", "contract_id"),
    [
        ("", "taskplanner-virtual-eir-nuc.v1"),
        ("/integration/virtual/surgery/controller_contract", ""),
    ],
)
def test_missing_controller_contract_metadata_does_not_block_emulator_endpoints(
    topic,
    contract_id,
):
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    emulator._servers = ["tool-action", "retraction-service"]
    emulator._controller_contract_topic = topic
    emulator._controller_contract_id = contract_id
    warnings = []
    emulator.get_logger = lambda: SimpleNamespace(warning=warnings.append)
    emulator.create_publisher = lambda *_args: (_ for _ in ()).throw(
        AssertionError("missing optional metadata created a publisher")
    )
    emulator.create_timer = lambda *_args: (_ for _ in ()).throw(
        AssertionError("missing optional metadata created a timer")
    )

    emulator._start_controller_contract_diagnostics()

    assert emulator._servers == ["tool-action", "retraction-service"]
    assert emulator._controller_contract_pub is None
    assert emulator._controller_contract_timer is None
    assert warnings


def test_invalid_controller_contract_topic_is_nonfatal_diagnostics() -> None:
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    emulator._servers = ["tool-action", "retraction-service"]
    emulator._controller_contract_topic = "not a ROS topic"
    emulator._controller_contract_id = "taskplanner-virtual-eir-nuc.v1"
    warnings = []
    emulator.get_logger = lambda: SimpleNamespace(warning=warnings.append)
    emulator.create_publisher = lambda *_args: (_ for _ in ()).throw(
        ValueError("invalid topic")
    )

    emulator._start_controller_contract_diagnostics()

    assert emulator._servers == ["tool-action", "retraction-service"]
    assert emulator._controller_contract_pub is None
    assert emulator._controller_contract_timer is None
    assert warnings == ["controller contract diagnostics disabled: ValueError"]


def test_configured_controller_contract_diagnostics_still_publish() -> None:
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    published = []
    publisher = SimpleNamespace(publish=published.append)
    timer = object()
    emulator._controller_contract_topic = (
        "/integration/virtual/surgery/controller_contract"
    )
    emulator._controller_contract_id = "taskplanner-virtual-eir-nuc.v1"
    emulator._robot_endpoint_source = "virtual"
    emulator._virtual_endpoint_mode = True
    emulator._tool_handover_endpoint = (
        "/integration/virtual/surgery/tool_handover"
    )
    emulator._retraction_service_name = (
        "/integration/virtual/surgery/retraction/command"
    )
    emulator._capability_policy_id = VIRTUAL_EMULATOR_CAPABILITY_POLICY_ID
    emulator.get_logger = lambda: SimpleNamespace(warning=lambda _message: None)
    emulator.create_publisher = lambda *_args: publisher
    emulator.create_timer = lambda *_args: timer

    emulator._start_controller_contract_diagnostics()

    assert emulator._controller_contract_pub is publisher
    assert emulator._controller_contract_timer is timer
    assert len(published) == 1
    assert json.loads(published[0].data)["contract_id"] == (
        "taskplanner-virtual-eir-nuc.v1"
    )


def test_retraction_service_is_immediate_admission_not_physical_result():
    emulator = _bare_emulator(
        Outcome(
            outcome="protective_stop",
            duration_sec=0.03,
            reason_code="guard_triggered",
        )
    )
    request = _command(command_id="service-1")
    response = SimpleNamespace(
        request_accepted=None,
        result_code=-1,
        command_id="",
        message="",
    )

    started = time.monotonic()
    result = emulator._request_retraction_command(request, response)
    elapsed = time.monotonic() - started

    assert elapsed < 0.025
    assert result.request_accepted is True
    assert result.result_code == ExecuteRetractionCommand.Response.RESULT_ACCEPTED
    assert result.command_id == "service-1"
    assert result.message == "guard_triggered"


def test_retraction_service_profile_rejects_without_claiming_execution():
    emulator = _bare_emulator(Outcome(outcome="reject", reason_code="controller_busy"))
    response = SimpleNamespace(
        request_accepted=None,
        result_code=-1,
        command_id="",
        message="",
    )

    result = emulator._request_retraction_command(_command(), response)

    assert result.request_accepted is False
    assert result.result_code == ExecuteRetractionCommand.Response.RESULT_REJECTED
    assert result.message == "controller_busy"
