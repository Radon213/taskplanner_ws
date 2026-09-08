from pathlib import Path
from types import SimpleNamespace

import pytest
import tool_belief_tracker.node as node_module
from rclpy.qos import DurabilityPolicy, ReliabilityPolicy
from tool_belief_tracker.core import BeliefTracker, InventoryItem, TrackerConfig
from tool_belief_tracker.node import (
    ToolBeliefTrackerNode,
    build_tracker_candidate,
    build_rehydrated_tracker,
    enabled_state_qos_profile,
    parameter_descriptor,
    parse_view_health,
    resolve_bundle_spec_dir,
    validate_spec_dir_under_root,
)


def _tracker(instrument_id: str, config: TrackerConfig | None = None) -> BeliefTracker:
    return BeliefTracker(
        [
            InventoryItem(
                instrument_id=instrument_id,
                instance_id=f"{instrument_id}#1",
                display_name=instrument_id,
                initial_location="tray",
            )
        ],
        config=config,
    )


def _fake_node():
    old_spec = SimpleNamespace(procedure_id="old")
    old_tracker = _tracker("OLD")
    fake = SimpleNamespace(
        _spec=old_spec,
        _tracker=old_tracker,
        _spec_dir="/specs/old",
        _bundle_id="old",
        _spec_root=Path("/specs"),
        _bundle_snapshot_root=Path("/tmp/taskplanner-procedure-snapshots"),
        _spec_inventory_digest="sha256:old",
        _procedure_run_id="run-old",
        _hydrated_run_id="run-old",
        _awaiting_rehydrate=False,
        _scenario_active=False,
        _scenario_running=False,
        _scenario_state_received=True,
        _scenario_config_bundle="",
        _state_sync_ready=True,
        _allow_active_spec_reconcile=False,
        _enabled=True,
        _enabled_publisher=SimpleNamespace(publish=lambda _message: None),
        _publish_period_sec=0.2,
        _health_freshness_sec=1.5,
        _timer=object(),
        _apply_pending_scenario_config_if_safe=lambda: None,
        get_logger=lambda: SimpleNamespace(
            info=lambda _message: None,
            error=lambda _message: None,
        ),
    )
    fake._publish_enabled_state = lambda: (
        ToolBeliefTrackerNode._publish_enabled_state(fake)
    )
    return fake, old_spec, old_tracker


def test_enabled_state_qos_is_reliable_and_transient_local() -> None:
    qos = enabled_state_qos_profile()
    assert qos.depth == 1
    assert qos.reliability == ReliabilityPolicy.RELIABLE
    assert qos.durability == DurabilityPolicy.TRANSIENT_LOCAL


def test_disable_pauses_without_destroying_current_belief() -> None:
    fake, _, old_tracker = _fake_node()
    published = []
    fake._enabled_publisher = SimpleNamespace(
        publish=lambda message: published.append(message.data)
    )

    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [SimpleNamespace(name="enabled", value=False)],
    )

    assert result.successful is True
    assert fake._enabled is False
    assert fake._tracker is old_tracker
    assert fake._state_sync_ready is True
    assert published == [False]


def test_reenable_discards_old_belief_and_waits_for_complete_state(monkeypatch) -> None:
    fake, _, old_tracker = _fake_node()
    fake._enabled = False
    fake._enabled_publisher = SimpleNamespace(publish=lambda _message: None)
    monkeypatch.setattr(
        node_module,
        "inventory_from_procedure_spec",
        lambda _spec: [
            InventoryItem("NEW", "NEW#1", "NEW", "tray"),
        ],
    )

    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [SimpleNamespace(name="enabled", value=True)],
    )

    assert result.successful is True
    assert fake._enabled is True
    assert fake._tracker is not old_tracker
    assert fake._tracker.instance_ids == ("NEW#1",)
    assert fake._procedure_run_id == ""
    assert fake._hydrated_run_id is None
    assert fake._awaiting_rehydrate is True
    assert fake._state_sync_ready is False


def test_repeated_enable_is_idempotent() -> None:
    fake, _, old_tracker = _fake_node()

    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [SimpleNamespace(name="enabled", value=True)],
    )

    assert result.successful is True
    assert fake._enabled is True
    assert fake._tracker is old_tracker
    assert fake._state_sync_ready is True


def test_disabled_callbacks_skip_evidence_and_snapshot_publication() -> None:
    calls = []
    fake = SimpleNamespace(
        _enabled=False,
        _tracker=SimpleNamespace(
            register_command=lambda *_args: calls.append("command"),
            apply_skill_status=lambda *_args: calls.append("status"),
            apply_semantic_event=lambda *_args, **_kwargs: calls.append("event"),
        ),
        _publisher=SimpleNamespace(publish=lambda _message: calls.append("publish")),
        _publish_enabled_state=lambda: calls.append("enabled_status"),
    )

    ToolBeliefTrackerNode._on_pose(fake, "cam_4", SimpleNamespace())
    ToolBeliefTrackerNode._on_skill_command(fake, SimpleNamespace())
    ToolBeliefTrackerNode._on_skill_status(fake, SimpleNamespace())
    ToolBeliefTrackerNode._on_skill_event(fake, SimpleNamespace())
    ToolBeliefTrackerNode._publish_snapshot(fake)

    assert calls == ["enabled_status"]


@pytest.mark.parametrize(
    ("event_type", "detail"),
    [
        (
            "ToolPrepared",
            {"authoritative_controller_completion": True},
        ),
        (
            "RobotTaskCompleted",
            {
                "transport": "ros2_action",
                "command_id": "cmd-1",
                "controller_final_state": "completed",
            },
        ),
    ],
)
def test_correlated_controller_completion_is_not_double_counted(
    event_type: str,
    detail: dict,
) -> None:
    calls = []
    fake = SimpleNamespace(
        _enabled=True,
        _procedure_run_id="run-current",
        _tracker=SimpleNamespace(
            apply_semantic_event=lambda **kwargs: calls.append(kwargs)
        ),
        _now_sec=lambda: 1.0,
        _resolve_instrument=lambda instrument_id: instrument_id,
        _canonical_instance=lambda instance_id, _instrument_id: instance_id,
    )
    message = SimpleNamespace(
        event_type=event_type,
        detail_json=node_module.json.dumps(detail),
        procedure_run_id="run-current",
        instrument_id="T04",
        instance_id="T04#1",
        location_id="robot_right_hand",
        target_location_id="robot",
        source_location_id="mayo",
        location_type="robot_right_hand",
        confidence=1.0,
    )

    ToolBeliefTrackerNode._on_skill_event(fake, message)

    assert calls == []


def test_authoritative_return_to_tray_reinforces_terminal_tray_evidence() -> None:
    calls = []
    fake = SimpleNamespace(
        _enabled=True,
        _procedure_run_id="run-current",
        _tracker=SimpleNamespace(
            apply_semantic_event=lambda **kwargs: calls.append(kwargs)
        ),
        _now_sec=lambda: 1.0,
        _resolve_instrument=lambda instrument_id: instrument_id,
        _canonical_instance=lambda instance_id, _instrument_id: instance_id,
    )
    message = SimpleNamespace(
        event_type="ToolReturnedToTray",
        detail_json=node_module.json.dumps(
            {"authoritative_controller_completion": True}
        ),
        procedure_run_id="run-current",
        instrument_id="T08",
        instance_id="T08#1",
        location_id="robot_left_hand",
        target_location_id="main_tray_slot_4",
        source_location_id="robot_left_hand",
        location_type="robot_left_hand",
        confidence=1.0,
    )

    ToolBeliefTrackerNode._on_skill_event(fake, message)

    assert calls == [
        {
            "instrument_id": "T08",
            "instance_id": "T08#1",
            "location": "tray",
            "confidence": 1.0,
            "event_type": "ToolReturnedToTray",
            "timestamp_sec": 1.0,
        }
    ]


def test_disabled_simulation_state_keeps_identity_and_self_heal_live() -> None:
    healed = []
    fake = SimpleNamespace(
        _enabled=False,
        _scenario_active=True,
        _scenario_state_received=False,
        _scenario_config_bundle="",
        _bundle_id="demo",
        _spec=SimpleNamespace(procedure_id="demo"),
        _now_sec=lambda: 1.0,
        _apply_pending_scenario_config_if_safe=lambda: None,
        _self_heal_active_bundle=lambda bundle: healed.append(bundle) or True,
    )
    message = SimpleNamespace(
        running=False,
        execution_state="idle",
        robot_state="idle",
        active_bundle="demo",
        procedure_id="demo",
    )

    ToolBeliefTrackerNode._on_simulation_state(fake, message)

    assert fake._scenario_active is False
    assert fake._scenario_state_received is True
    assert healed == ["demo"]


def test_enabled_idle_state_rehydrates_without_a_procedure_run() -> None:
    hydrated = []
    motion = []
    fake = SimpleNamespace(
        _enabled=True,
        _scenario_active=True,
        _scenario_state_received=False,
        _scenario_config_bundle="",
        _bundle_id="demo",
        _spec=SimpleNamespace(procedure_id="demo"),
        _tracker=SimpleNamespace(
            set_robot_motion=lambda active, now, task_id: motion.append(
                (active, now, task_id)
            ),
        ),
        _now_sec=lambda: 12.0,
        _apply_pending_scenario_config_if_safe=lambda: None,
        _self_heal_active_bundle=lambda _bundle: True,
        _rehydrate_from_simulation_state=lambda message: hydrated.append(message),
    )
    message = SimpleNamespace(
        running=False,
        execution_state="idle",
        robot_state="idle",
        active_bundle="demo",
        procedure_id="demo",
        procedure_run_id="",
        active_robot_task_id="",
        active_robot_task_tool_id="",
    )

    ToolBeliefTrackerNode._on_simulation_state(fake, message)

    assert fake._scenario_active is False
    assert fake._scenario_state_received is True
    assert hydrated == [message]
    assert motion == [(False, 12.0, "")]


@pytest.mark.parametrize(
    ("received", "running", "execution_state", "allowed"),
    [
        (False, False, "", True),
        (True, True, "paused", True),
        (True, False, "idle", True),
        (True, False, "halted", True),
        (True, True, "running", False),
        # A malformed or absent state label must not make an active scenario
        # look like a safe ScenarioStore application boundary.
        (True, True, "", False),
    ],
)
def test_scenario_config_refresh_accepts_only_quiescent_boundaries(
    received: bool,
    running: bool,
    execution_state: str,
    allowed: bool,
) -> None:
    fake = SimpleNamespace(
        _scenario_state_received=received,
        _scenario_running=running,
        _scenario_execution_state=execution_state,
    )

    assert ToolBeliefTrackerNode._scenario_config_apply_is_safe(fake) is allowed


def test_set_enabled_service_uses_the_atomic_parameter_path() -> None:
    requested = []
    fake = SimpleNamespace(
        set_parameters_atomically=lambda parameters: (
            requested.extend(parameters)
            or SimpleNamespace(successful=True, reason="")
        )
    )
    response = SimpleNamespace(success=False, message="")

    returned = ToolBeliefTrackerNode._handle_set_enabled(
        fake,
        SimpleNamespace(data=False),
        response,
    )

    assert returned is response
    assert response.success is True
    assert response.message == "tool belief tracker disabled"
    assert [(parameter.name, parameter.value) for parameter in requested] == [
        ("enabled", False)
    ]


def test_parameter_metadata_exposes_restart_and_runtime_constraints() -> None:
    immutable = parameter_descriptor(
        "topology",
        constraints="restart required",
        read_only=True,
    )
    dynamic = parameter_descriptor(
        "negative scale",
        constraints="finite and in [0, 1]",
    )

    assert immutable.read_only is True
    assert immutable.additional_constraints == "restart required"
    assert dynamic.read_only is False
    assert dynamic.additional_constraints == "finite and in [0, 1]"


def test_failed_spec_reload_preserves_old_runtime_state(monkeypatch) -> None:
    fake, old_spec, old_tracker = _fake_node()

    def reject_candidate(_spec_dir, _config):
        raise ValueError("invalid bundle")

    monkeypatch.setattr(node_module, "build_tracker_candidate", reject_candidate)
    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [SimpleNamespace(name="spec_dir", value="/specs/bad")],
    )

    assert result.successful is False
    assert "before swap" in result.reason
    assert fake._spec is old_spec
    assert fake._tracker is old_tracker
    assert fake._spec_dir == "/specs/old"
    assert fake._procedure_run_id == "run-old"


def test_invalid_numeric_batch_preserves_complete_runtime_state() -> None:
    fake, old_spec, old_tracker = _fake_node()
    old_config = old_tracker.config
    old_timer = fake._timer
    old_revision = ToolBeliefTrackerNode._tracker_revision(fake)

    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [
            SimpleNamespace(name="positive_gain", value=4.5),
            SimpleNamespace(name="confirm_threshold", value=0.4),
            SimpleNamespace(name="probable_threshold", value=0.7),
        ],
    )

    assert result.successful is False
    assert fake._spec is old_spec
    assert fake._tracker is old_tracker
    assert fake._tracker.config == old_config
    assert fake._timer is old_timer
    assert fake._publish_period_sec == 0.2
    assert fake._health_freshness_sec == 1.5
    assert ToolBeliefTrackerNode._tracker_revision(fake) == old_revision


def test_valid_numeric_batch_updates_model_without_spec_or_timer_swap() -> None:
    fake, old_spec, old_tracker = _fake_node()
    old_timer = fake._timer
    old_revision = ToolBeliefTrackerNode._tracker_revision(fake)

    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [
            SimpleNamespace(name="positive_gain", value=4.5),
            SimpleNamespace(name="miss_half_life_sec", value=5.0),
            SimpleNamespace(name="health_freshness_sec", value=2.0),
        ],
    )

    assert result.successful is True
    assert fake._spec.procedure_id == old_spec.procedure_id
    assert fake._tracker is old_tracker
    assert fake._tracker.config.positive_gain == 4.5
    assert fake._tracker.config.miss_half_life_sec == 5.0
    assert fake._health_freshness_sec == 2.0
    assert fake._timer is old_timer
    assert ToolBeliefTrackerNode._tracker_revision(fake) != old_revision


def test_publish_timer_allocation_failure_rolls_back_numeric_batch() -> None:
    fake, old_spec, old_tracker = _fake_node()
    old_config = old_tracker.config
    old_timer = fake._timer
    fake.create_timer = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("timer allocation failed")
    )

    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [
            SimpleNamespace(name="positive_gain", value=4.5),
            SimpleNamespace(name="publish_period_sec", value=0.1),
        ],
    )

    assert result.successful is False
    assert "replace publish timer" in result.reason
    assert fake._spec is old_spec
    assert fake._tracker is old_tracker
    assert fake._tracker.config == old_config
    assert fake._timer is old_timer
    assert fake._publish_period_sec == 0.2


def test_valid_spec_and_tuning_batch_swaps_once_and_clears_old_run(monkeypatch) -> None:
    fake, old_spec, old_tracker = _fake_node()
    candidate_spec = SimpleNamespace(procedure_id="new")
    captured = {}

    def build_candidate(spec_dir, config):
        captured["spec_dir"] = spec_dir
        captured["config"] = config
        return candidate_spec, _tracker("NEW", config), "sha256:new"

    monkeypatch.setattr(node_module, "build_tracker_candidate", build_candidate)
    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [
            SimpleNamespace(name="spec_dir", value="/specs/new"),
            SimpleNamespace(name="positive_gain", value=4.5),
        ],
    )

    assert result.successful is True
    assert captured["spec_dir"] == "/specs/new"
    assert captured["config"].positive_gain == 4.5
    assert fake._spec is candidate_spec
    assert fake._tracker is not old_tracker
    assert fake._tracker.instance_ids == ("NEW#1",)
    assert fake._spec_inventory_digest == "sha256:new"
    assert fake._bundle_id == "new"
    assert fake._procedure_run_id == ""
    assert fake._awaiting_rehydrate is True
    assert fake._state_sync_ready is False
    assert old_spec.procedure_id == "old"


def test_hot_identity_mode_change_rebuilds_and_waits_for_fresh_state(
    monkeypatch,
) -> None:
    fake, _, old_tracker = _fake_node()
    inventory = [
        InventoryItem(
            "T03",
            "T03#1",
            "Adson",
            "tray",
            exchangeable_population=True,
        ),
        InventoryItem(
            "T03",
            "T03#2",
            "Adson",
            "unknown",
            initial_confidence=0.0,
            initial_activity_probability=0.0,
            exchangeable_population=True,
        ),
    ]
    monkeypatch.setattr(
        node_module,
        "inventory_from_procedure_spec",
        lambda _spec: inventory,
    )

    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [SimpleNamespace(name="exchangeable_instances", value=True)],
    )

    assert result.successful is True
    assert fake._tracker is not old_tracker
    assert fake._tracker.config.exchangeable_instances is True
    assert fake._tracker.instance_ids == ("T03#1", "T03#2")
    assert fake._awaiting_rehydrate is True
    assert fake._state_sync_ready is False
    assert fake._procedure_run_id == ""


def test_startup_candidate_rejects_strict_mode_for_expanded_capacity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    spec = SimpleNamespace(
        procedure_id=tmp_path.name,
        get_tool_inventory=lambda: {"T03": 1},
        get_tool_inventory_capacity=lambda: {"T03": 2},
    )
    monkeypatch.setattr(node_module, "load_bundle", lambda _path: spec)
    monkeypatch.setattr(
        node_module,
        "inventory_from_procedure_spec",
        lambda _spec: (_ for _ in ()).throw(
            AssertionError("invalid identity mode must fail before inventory build")
        ),
    )

    with pytest.raises(ValueError, match="cannot represent procedure capacity"):
        build_tracker_candidate(str(tmp_path), TrackerConfig())


def test_stopped_reload_preserves_current_tracker_when_candidate_requires_exchangeable(
    monkeypatch,
) -> None:
    fake, old_spec, old_tracker = _fake_node()
    monkeypatch.setattr(
        node_module,
        "build_tracker_candidate",
        lambda *_args: (_ for _ in ()).throw(
            ValueError(
                "exchangeable_instances=false cannot represent procedure capacity"
            )
        ),
    )

    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [SimpleNamespace(name="spec_dir", value="/specs/expanded")],
    )

    assert result.successful is False
    assert "cannot represent procedure capacity" in result.reason
    assert fake._spec is old_spec
    assert fake._tracker is old_tracker


def test_hot_disable_exchangeable_is_rejected_for_current_expanded_capacity() -> None:
    fake, _, _ = _fake_node()
    expanded_spec = SimpleNamespace(
        procedure_id="expanded",
        get_tool_inventory=lambda: {"T03": 1},
        get_tool_inventory_capacity=lambda: {"T03": 2},
    )
    current_tracker = BeliefTracker(
        [
            InventoryItem(
                "T03",
                "T03#1",
                "Adson",
                "tray",
                exchangeable_population=True,
            ),
            InventoryItem(
                "T03",
                "T03#2",
                "Adson",
                "unknown",
                initial_confidence=0.0,
                initial_activity_probability=0.0,
                exchangeable_population=True,
            ),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )
    fake._spec = expanded_spec
    fake._tracker = current_tracker

    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [SimpleNamespace(name="exchangeable_instances", value=False)],
    )

    assert result.successful is False
    assert "cannot represent procedure capacity" in result.reason
    assert fake._tracker is current_tracker
    assert fake._tracker.config.exchangeable_instances is True


def test_direct_spec_reload_is_rejected_while_scenario_active(monkeypatch) -> None:
    fake, old_spec, old_tracker = _fake_node()
    fake._scenario_active = True
    monkeypatch.setattr(
        node_module,
        "build_tracker_candidate",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not load")),
    )

    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [SimpleNamespace(name="spec_dir", value="/specs/new")],
    )

    assert result.successful is False
    assert "stopped" in result.reason
    assert fake._spec is old_spec
    assert fake._tracker is old_tracker


def test_direct_spec_reload_is_fail_closed_before_first_simulation_state(
    monkeypatch,
) -> None:
    fake, old_spec, old_tracker = _fake_node()
    fake._scenario_state_received = False
    monkeypatch.setattr(
        node_module,
        "build_tracker_candidate",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not load")),
    )

    result = ToolBeliefTrackerNode._on_parameters_changed(
        fake,
        [SimpleNamespace(name="spec_dir", value="/specs/new")],
    )

    assert result.successful is False
    assert "fresh stopped" in result.reason
    assert fake._spec is old_spec
    assert fake._tracker is old_tracker


def test_optional_participant_self_heals_from_active_bundle(tmp_path) -> None:
    fake, _, _ = _fake_node()
    fake._spec_root = tmp_path
    (tmp_path / "new").mkdir()
    requested = []

    def set_parameters_atomically(parameters):
        requested.append(parameters[0].value)
        fake._spec = SimpleNamespace(procedure_id="new")
        fake._bundle_id = "new"
        return SimpleNamespace(successful=True, reason="")

    fake.set_parameters_atomically = set_parameters_atomically

    assert ToolBeliefTrackerNode._self_heal_active_bundle(fake, "new") is True
    assert requested == [str((tmp_path / "new").resolve())]
    assert fake._allow_active_spec_reconcile is False


def test_bundle_self_heal_path_rejects_traversal(tmp_path) -> None:
    assert resolve_bundle_spec_dir(tmp_path, "thyroidectomy_demo") == (
        tmp_path / "thyroidectomy_demo"
    ).resolve()
    for invalid in ("../outside", "nested/name", "..", ".hidden"):
        try:
            resolve_bundle_spec_dir(tmp_path, invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid bundle: {invalid}")


def test_spec_reload_path_accepts_source_child_and_canonical_snapshot(
    tmp_path,
) -> None:
    spec_root = tmp_path / "source" / "specs"
    snapshot_root = tmp_path / "snapshots"
    authored = spec_root / "thyroidectomy_demo"
    snapshot = (
        snapshot_root
        / "thyroidectomy_demo"
        / ("a" * 64)
        / "specs"
        / "thyroidectomy_demo"
    )

    assert validate_spec_dir_under_root(
        spec_root,
        str(authored),
        snapshot_root,
    ) == authored.resolve()
    assert validate_spec_dir_under_root(
        spec_root,
        str(snapshot),
        snapshot_root,
    ) == snapshot.resolve()


@pytest.mark.parametrize(
    "relative",
    [
        "thyroidectomy_demo/not-a-digest/specs/thyroidectomy_demo",
        f"thyroidectomy_demo/{'a' * 64}/wrong/thyroidectomy_demo",
        f"thyroidectomy_demo/{'a' * 64}/specs/nephrectomy",
        f"extra/thyroidectomy_demo/{'a' * 64}/specs/thyroidectomy_demo",
    ],
)
def test_spec_reload_path_rejects_noncanonical_snapshot(tmp_path, relative) -> None:
    with pytest.raises(ValueError, match="canonical manager-owned"):
        validate_spec_dir_under_root(
            tmp_path / "source" / "specs",
            str(tmp_path / "snapshots" / relative),
            tmp_path / "snapshots",
        )


def test_active_bundle_self_heal_remains_on_authored_spec_root(tmp_path) -> None:
    spec_root = tmp_path / "source" / "specs"
    snapshot_root = tmp_path / "snapshots"

    assert resolve_bundle_spec_dir(spec_root, "thyroidectomy_demo") == (
        spec_root / "thyroidectomy_demo"
    ).resolve()
    assert resolve_bundle_spec_dir(spec_root, "thyroidectomy_demo") != (
        snapshot_root
        / "thyroidectomy_demo"
        / ("a" * 64)
        / "specs"
        / "thyroidectomy_demo"
    ).resolve()


def test_tracker_candidate_rejects_directory_procedure_identity_split(
    tmp_path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "thyroidectomy"
    candidate.mkdir()
    monkeypatch.setattr(
        node_module,
        "load_bundle",
        lambda _path: SimpleNamespace(procedure_id="nephrectomy"),
    )

    with pytest.raises(ValueError, match="does not match loaded procedure_id"):
        build_tracker_candidate(str(candidate), TrackerConfig())


def test_explicit_semantic_not_ready_overrides_ready_true() -> None:
    ready, zone = parse_view_health(
        {
            "ready": True,
            "semantic_ready": False,
            "tf_workspace_zone": "tray",
        },
        fallback_zone="tray",
        view="cam_3",
    )
    assert ready is False
    assert zone == "tray"


def test_pose_audits_catalog_known_but_out_of_inventory_per_detection() -> None:
    tracker = BeliefTracker(
        [
            InventoryItem("T02", "T02#1", "T02", "tray"),
            InventoryItem("T03", "T03#1", "T03", "field"),
            InventoryItem("T03", "T03#2", "T03", "field"),
            InventoryItem("T04", "T04#1", "T04", "tray"),
            InventoryItem("T07", "T07#1", "T07", "tray"),
        ]
    )
    resolved = {
        "Bovie": "T04",
        "Mosquito": "T08",
        "provider-unknown": "",
    }
    fake = SimpleNamespace(
        _enabled=True,
        _tracker=tracker,
        _health={
            "cam_4": SimpleNamespace(
                ready=True,
                received_sec=0.5,
                zone="mayo",
            )
        },
        _health_freshness_sec=1.5,
        _now_sec=lambda: 1.0,
        _resolve_instrument=lambda class_name: resolved[class_name],
    )
    message = SimpleNamespace(
        source_view="cam_4",
        tools=[
            SimpleNamespace(class_name="Bovie", class_confidence=0.9),
            SimpleNamespace(class_name="Mosquito", class_confidence=0.8),
            SimpleNamespace(class_name="Mosquito", class_confidence=0.7),
            SimpleNamespace(
                class_name="provider-unknown",
                class_confidence=0.6,
            ),
        ],
        model_version="model",
        ontology_version="ontology",
        calibration_version="calibration",
    )

    ToolBeliefTrackerNode._on_pose(fake, "cam_4", message)

    assert tracker.instance_ids == (
        "T02#1",
        "T03#1",
        "T03#2",
        "T04#1",
        "T07#1",
    )
    assert tracker.ignored_out_of_inventory_count == 3
    assert tracker.ignored_class_names == ("Mosquito", "provider-unknown")
    bovie = next(
        belief
        for belief in tracker.snapshot(1.0)
        if belief["instance_id"] == "T04#1"
    )
    assert bovie["locations"]["mayo"] > 0.0


def test_pose_forwards_valid_observation_uv_and_ignores_invalid_uv() -> None:
    received = []

    class CapturingTracker:
        instrument_ids = ("T02", "T04")

        def note_ignored_class(self, _class_name):
            raise AssertionError("all fixture classes are in inventory")

        def apply_camera_frame(self, **kwargs):
            received.extend(kwargs["detections"])

    fake = SimpleNamespace(
        _enabled=True,
        _tracker=CapturingTracker(),
        _health={
            "cam_4": SimpleNamespace(
                ready=True,
                received_sec=0.5,
                zone="mayo",
            )
        },
        _health_freshness_sec=1.5,
        _now_sec=lambda: 1.0,
        _resolve_instrument=lambda class_name: {
            "Adson": "T02",
            "Bovie": "T04",
        }[class_name],
    )
    message = SimpleNamespace(
        source_view="cam_4",
        tools=[
            SimpleNamespace(
                class_name="Adson",
                class_confidence=0.8,
                observation_point_uv_px=[790.0, 288.0],
                observation_point_inside_mask=True,
            ),
            SimpleNamespace(
                class_name="Bovie",
                class_confidence=0.9,
                observation_point_uv_px=[float("nan"), 424.0],
                observation_point_inside_mask=True,
            ),
        ],
        model_version="model",
        ontology_version="ontology",
        calibration_version="calibration",
    )

    ToolBeliefTrackerNode._on_pose(fake, "cam_4", message)

    assert received == [
        ("T02", 0.8, 790.0, 288.0),
        ("T04", 0.9),
    ]


def test_start_uses_pre_run_operator_verified_belief_without_detector_recapture() -> None:
    active = InventoryItem(
        "T04",
        "T04#1",
        "Bovie",
        "mayo",
        exchangeable_population=True,
    )
    dormant = InventoryItem(
        "T04",
        "T04#2",
        "Bovie",
        "unknown",
        initial_confidence=0.0,
        initial_activity_probability=0.0,
        exchangeable_population=True,
    )
    fake = SimpleNamespace(
        _tracker=BeliefTracker(
            [active, dormant],
            config=TrackerConfig(exchangeable_instances=True),
        ),
        _procedure_run_id="",
        _hydrated_run_id="",
        _awaiting_rehydrate=False,
        _state_sync_ready=True,
        _locked_inventory_run_id="",
        get_logger=lambda: SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
        ),
    )

    assert ToolBeliefTrackerNode._freeze_operator_verified_run_prior(fake, "run-42")
    assert fake._tracker.instance_ids == ("T04#1",)
    assert fake._procedure_run_id == "run-42"
    assert fake._locked_inventory_run_id == "run-42"
    assert fake._state_sync_ready is True
    # Repeated state frames must not rebuild or re-capture the population.
    frozen = fake._tracker
    assert not ToolBeliefTrackerNode._freeze_operator_verified_run_prior(fake, "run-42")
    assert fake._tracker is frozen


def test_start_prior_requires_a_previously_published_live_belief() -> None:
    fake = SimpleNamespace(
        _tracker=_tracker("T04", TrackerConfig(exchangeable_instances=True)),
        _state_sync_ready=False,
        _locked_inventory_run_id="",
        get_logger=lambda: SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
        ),
    )

    assert not ToolBeliefTrackerNode._freeze_operator_verified_run_prior(fake, "run-43")
    assert fake._tracker.instance_ids == ("T04#1",)


def test_tracker_revision_changes_with_spec_inventory_digest() -> None:
    fake, _, _ = _fake_node()

    first = ToolBeliefTrackerNode._tracker_revision(fake)
    fake._spec_inventory_digest = "sha256:different-inventory"
    second = ToolBeliefTrackerNode._tracker_revision(fake)

    assert first != second


def test_tracker_candidate_digest_covers_capacity_and_initial_activity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    mutable = {
        "capacity": 1,
        "activity": 1.0,
        "exchangeable": True,
    }
    spec = SimpleNamespace(
        procedure_id=tmp_path.name,
        get_tool_inventory=lambda: {"T03": 1},
        get_tool_inventory_capacity=lambda: {"T03": mutable["capacity"]},
    )

    def inventory(_spec):
        return [
            InventoryItem(
                instrument_id="T03",
                instance_id="T03#1",
                display_name="Adson",
                initial_location="tray",
                initial_activity_probability=mutable["activity"],
                exchangeable_population=mutable["exchangeable"],
            )
        ]

    monkeypatch.setattr(node_module, "load_bundle", lambda _path: spec)
    monkeypatch.setattr(node_module, "inventory_from_procedure_spec", inventory)

    exchangeable_config = TrackerConfig(exchangeable_instances=True)
    _, _, baseline = build_tracker_candidate(str(tmp_path), exchangeable_config)
    mutable["capacity"] = 2
    _, _, capacity_changed = build_tracker_candidate(
        str(tmp_path),
        exchangeable_config,
    )
    mutable["capacity"] = 1
    mutable["activity"] = 0.0
    _, activity_tracker, activity_changed = build_tracker_candidate(
        str(tmp_path),
        exchangeable_config,
    )
    mutable["activity"] = 1.0
    mutable["exchangeable"] = False
    _, _, exchangeability_changed = build_tracker_candidate(
        str(tmp_path),
        exchangeable_config,
    )

    assert capacity_changed != baseline
    assert activity_changed != baseline
    assert exchangeability_changed != baseline
    activity_belief = activity_tracker.snapshot(0.0)[0]
    assert activity_belief["existence_probability"] == 0.0
    assert "capacity_slot_inactive" in activity_belief["status_flags"]


def test_same_run_hydrates_once_and_each_new_run_resets(monkeypatch) -> None:
    fake, _, old_tracker = _fake_node()
    fake._awaiting_rehydrate = True
    fake._hydrated_run_id = None
    fake._state_sync_ready = False
    calls = []

    def rehydrate(_spec, config, instrument_states):
        calls.append(tuple(instrument_states))
        replacement = _tracker(f"RUN{len(calls)}", config)
        return replacement, True

    monkeypatch.setattr(node_module, "build_rehydrated_tracker", rehydrate)
    run_one = SimpleNamespace(procedure_run_id="run-1", instrument_states=["one"])
    run_two = SimpleNamespace(procedure_run_id="run-2", instrument_states=["two"])
    idle_run = SimpleNamespace(procedure_run_id="", instrument_states=["idle"])

    ToolBeliefTrackerNode._rehydrate_from_simulation_state(fake, run_one)
    first_tracker = fake._tracker
    assert fake._state_sync_ready is True
    ToolBeliefTrackerNode._rehydrate_from_simulation_state(fake, run_one)
    assert fake._tracker is first_tracker
    assert len(calls) == 1

    ToolBeliefTrackerNode._rehydrate_from_simulation_state(fake, run_two)
    assert fake._tracker is not first_tracker
    assert fake._tracker.instance_ids == ("RUN2#1",)
    assert fake._hydrated_run_id == "run-2"

    ToolBeliefTrackerNode._rehydrate_from_simulation_state(fake, idle_run)
    assert fake._tracker.instance_ids == ("RUN3#1",)
    assert fake._hydrated_run_id == ""
    assert old_tracker.instance_ids == ("OLD#1",)
    assert len(calls) == 3


def test_incomplete_new_run_discards_previous_run_and_retries(monkeypatch) -> None:
    fake, _, previous_tracker = _fake_node()
    reset_tracker = _tracker("RESET")
    calls = 0

    def incomplete(_spec, _config, _instrument_states):
        nonlocal calls
        calls += 1
        return reset_tracker, False

    monkeypatch.setattr(node_module, "build_rehydrated_tracker", incomplete)
    message = SimpleNamespace(procedure_run_id="run-new", instrument_states=[])

    ToolBeliefTrackerNode._rehydrate_from_simulation_state(fake, message)
    assert fake._tracker is reset_tracker
    assert fake._tracker is not previous_tracker
    assert fake._awaiting_rehydrate is True
    assert fake._hydrated_run_id is None
    assert fake._state_sync_ready is False

    ToolBeliefTrackerNode._rehydrate_from_simulation_state(fake, message)
    assert calls == 2


def test_mismatched_procedure_state_cannot_rehydrate_tracker() -> None:
    fake, _, old_tracker = _fake_node()
    fake._now_sec = lambda: 1.0
    fake._self_heal_active_bundle = lambda _bundle: True
    hydrated = []
    fake._rehydrate_from_simulation_state = lambda message: hydrated.append(message)
    message = SimpleNamespace(
        running=False,
        execution_state="idle",
        robot_state="idle",
        active_bundle="old",
        procedure_id="different-procedure",
    )

    ToolBeliefTrackerNode._on_simulation_state(fake, message)

    assert hydrated == []
    assert fake._tracker is old_tracker


def test_publish_is_suppressed_until_fixed_state_rehydration() -> None:
    status_heartbeats = []
    fake = SimpleNamespace(
        _enabled=True,
        _state_sync_ready=False,
        _now_sec=lambda: 0.0,
        _publish_enabled_state=lambda: status_heartbeats.append(True),
        _publisher=SimpleNamespace(
            publish=lambda _message: (_ for _ in ()).throw(
                AssertionError("unsynchronized snapshot must not publish")
            )
        ),
    )

    ToolBeliefTrackerNode._publish_snapshot(fake)

    assert status_heartbeats == [True]


def test_node_respawn_and_run_change_rehydrate_from_simulation_state() -> None:
    source_root = Path(__file__).resolve().parents[2]
    spec_dir = (
        source_root
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    spec = node_module.load_bundle(spec_dir)
    instrument_states = []
    for item in node_module.inventory_from_procedure_spec(spec):
        instrument_states.append(
            SimpleNamespace(
                instrument_id=item.instrument_id,
                instance_id=item.instance_id,
                location_id="mayo_stand",
                location_type="mayo",
                confidence=1.0,
            )
        )

    tracker, complete = build_rehydrated_tracker(
        spec,
        TrackerConfig(),
        instrument_states,
    )

    assert complete is True
    for belief in tracker.snapshot(1.0):
        assert belief["most_likely_location_id"] == "mayo"
        assert belief["most_likely_probability"] > 0.85
        assert "simulation_state" in belief["evidence_sources"]


def test_exchangeable_rehydrate_accepts_only_dt_initial_count_and_keeps_capacity_dormant() -> None:
    source_root = Path(__file__).resolve().parents[2]
    spec_dir = (
        source_root
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    spec = node_module.load_bundle(spec_dir)
    active_states = []
    for item in node_module.inventory_from_procedure_spec(spec):
        if item.initial_activity_probability == 0.0:
            continue
        active_states.append(
            SimpleNamespace(
                instrument_id=item.instrument_id,
                instance_id=item.instance_id,
                location_id="instrument_rack",
                location_type="instrument_rack",
                confidence=1.0,
            )
        )

    tracker, complete = build_rehydrated_tracker(
        spec,
        TrackerConfig(exchangeable_instances=True),
        active_states,
    )
    strict_tracker, strict_complete = build_rehydrated_tracker(
        spec,
        TrackerConfig(exchangeable_instances=False),
        active_states,
    )

    assert complete is True
    assert strict_complete is False
    assert strict_tracker.instance_ids == tracker.instance_ids
    beliefs = {row["instance_id"]: row for row in tracker.snapshot(0.0)}
    assert beliefs["T02#1"]["existence_probability"] >= 0.90
    assert beliefs["T02#2"]["existence_probability"] == 0.0
    assert "capacity_slot_inactive" in beliefs["T02#2"]["status_flags"]
