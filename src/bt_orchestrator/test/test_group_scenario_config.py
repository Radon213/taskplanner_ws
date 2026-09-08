from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from procedure_spec import (
    compute_bundle_config_revision,
    load_bundle,
    parse_scenario_config,
    scenario_config_payload,
)
from rclpy.qos import DurabilityPolicy, ReliabilityPolicy
from std_msgs.msg import String

from bt_orchestrator.bed_robot_arm_group_orchestrator import (
    BedRobotArmGroupOrchestrator,
    scenario_config_qos_profile,
)


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warning(self, message: str, **_kwargs) -> None:
        self.warnings.append(message)

    def info(self, message: str, **_kwargs) -> None:
        self.infos.append(message)


def _spec_dir(bundle_name: str) -> Path:
    return (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / bundle_name
    ).resolve()


def _scenario_message(
    bundle_name: str,
    *,
    spec_dir: Path | None = None,
    revision: str | None = None,
) -> String:
    message = String()
    candidate = spec_dir or _spec_dir(bundle_name)
    message.data = json.dumps(
        scenario_config_payload(
            bundle_name=bundle_name,
            spec_dir=str(candidate),
            revision=revision or compute_bundle_config_revision(candidate),
        )
    )
    return message


def _router() -> BedRobotArmGroupOrchestrator:
    router = BedRobotArmGroupOrchestrator.__new__(BedRobotArmGroupOrchestrator)
    initial_spec_dir = _spec_dir("thyroidectomy")
    router._spec_dir = str(initial_spec_dir)
    router._spec = load_bundle(initial_spec_dir)
    router._scenario_config_root = initial_spec_dir.parent
    router._scenario_config_revision = ""
    router._pending_scenario_config = None
    router._latest_simulation_state = None
    router._inflight_commands = {}
    router._pending_retraction = None
    router._group_states = {}
    router._last_lifecycle_control_signature = None
    router._clear_runtime_state = lambda: None
    logger = _Logger()
    router.get_logger = lambda: logger
    router._test_logger = logger
    return router


def test_scenario_config_subscription_is_transient_local() -> None:
    profile = scenario_config_qos_profile()

    assert profile.durability == DurabilityPolicy.TRANSIENT_LOCAL
    assert profile.reliability == ReliabilityPolicy.RELIABLE


def test_scenario_config_waits_for_paused_state_then_swaps_local_spec() -> None:
    router = _router()
    router._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="running",
    )

    router._on_scenario_config(_scenario_message("thyroidectomy_demo"))

    assert router._spec.procedure_id == "thyroidectomy"
    assert router._pending_scenario_config is not None

    router._on_simulation_state(
        SimpleNamespace(running=True, execution_state="paused")
    )

    assert router._spec.procedure_id == "thyroidectomy_demo"
    assert router._pending_scenario_config is None
    assert router._scenario_config_revision == compute_bundle_config_revision(
        _spec_dir("thyroidectomy_demo")
    )


def test_scenario_config_waits_until_local_group_work_is_quiet() -> None:
    router = _router()
    router._latest_simulation_state = SimpleNamespace(
        running=False,
        execution_state="idle",
    )
    router._inflight_commands = {"retraction": object()}

    router._on_scenario_config(_scenario_message("thyroidectomy_demo"))

    assert router._spec.procedure_id == "thyroidectomy"
    assert router._pending_scenario_config is not None

    router._inflight_commands.clear()
    router._apply_pending_scenario_config_if_safe()

    assert router._spec.procedure_id == "thyroidectomy_demo"
    assert router._pending_scenario_config is None


def test_scenario_config_waits_for_controller_reported_group_motion() -> None:
    router = _router()
    router._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="paused",
    )
    router._group_states = {
        "retraction": SimpleNamespace(
            active_request_id="",
            active_command_id="",
            state="retracting",
        )
    }

    router._on_scenario_config(_scenario_message("thyroidectomy_demo"))

    assert router._spec.procedure_id == "thyroidectomy"
    assert router._pending_scenario_config is not None

    router._group_states["retraction"].state = "standby"
    router._apply_pending_scenario_config_if_safe()

    assert router._spec.procedure_id == "thyroidectomy_demo"
    assert router._pending_scenario_config is None


def test_scenario_config_accepts_an_explicit_stopped_lifecycle_state() -> None:
    router = _router()
    router._latest_simulation_state = SimpleNamespace(
        running=False,
        execution_state="stopped",
    )

    router._on_scenario_config(_scenario_message("thyroidectomy_demo"))

    assert router._spec.procedure_id == "thyroidectomy_demo"
    assert router._pending_scenario_config is None


def test_scenario_config_rejects_a_path_outside_its_fixed_root() -> None:
    router = _router()
    snapshot = parse_scenario_config(
        _scenario_message(
            "thyroidectomy_demo",
            spec_dir=Path("/tmp/not-a-taskplanner-procedure"),
            revision="sha256:" + "0" * 64,
        ).data
    )

    with pytest.raises(ValueError, match="outside the fixed root"):
        router._scenario_config_candidate(snapshot)
