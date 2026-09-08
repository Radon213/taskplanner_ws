from pathlib import Path

import pytest

from integration_debug.contracts import (
    VALID_TOOL_TRANSITIONS,
    action_watchdog_reason,
    decode_payload,
    load_action_watchdog_policy,
    load_config,
    manual_write_block_reason,
    measured_rate,
    operational_runtime_intervention_block_reason,
    operational_runtime_stopped,
    operational_state_publisher_trusted,
    parse_voice_command,
    validate_action_recovery_acknowledgement,
    validate_planner_coexistence_acknowledgement,
    validate_bed_robot_arm_status,
    validate_retraction_command,
    validate_tool_handover,
)


VOICE = {
    "default_source_location": "tray",
    "default_target_location": "surgeon",
    "aliases": {"켈리": "Kelly forceps", "보비": "Bovie surgical cautery"},
}


def test_validates_only_public_handover_transitions() -> None:
    assert VALID_TOOL_TRANSITIONS == {
        ("tray", "robot"),
        ("mayo", "robot"),
        ("tray", "surgeon"),
        ("robot", "surgeon"),
        ("robot", "tray"),
        ("robot", "mayo"),
        ("mayo", "tray"),
    }
    mapped = validate_tool_handover(
        {
            "instrument_id": "Kelly forceps",
            "source_location": "tray",
            "target_location": "surgeon",
        }
    )
    assert mapped["instrument_instance_id"] == "Kelly forceps#1"
    mayo_prepare = validate_tool_handover(
        {
            "instrument_id": "Kelly forceps",
            "source_location": "mayo",
            "target_location": "robot",
        }
    )
    assert mayo_prepare["source_location"] == "mayo"
    assert mayo_prepare["target_location"] == "robot"
    with pytest.raises(ValueError, match="unsupported"):
        validate_tool_handover(
            {
                "instrument_id": "Kelly forceps",
                "source_location": "surgeon",
                "target_location": "robot",
            }
        )


def test_retraction_command_uses_only_the_single_service_contract() -> None:
    adjustment = validate_retraction_command(
        {
            "command": "adjust_retraction",
            "target_side": "left",
            "distance_m": 0.05,
        }
    )
    assert adjustment == {
        "command": "adjust_retraction",
        "target_side": "left",
        "distance_m": 0.05,
    }
    assert validate_retraction_command(
        {
            "command": "start_direct_teach",
            "target_side": "none",
            "distance_m": 0,
        }
    ) == {
        "command": "start_direct_teach",
        "target_side": "none",
        "distance_m": 0.0,
    }
    with pytest.raises(ValueError, match="unsupported retraction command fields"):
        validate_retraction_command(
            {
                **adjustment,
                "direction": "left",
                "axis": "left_right",
                "arm_id": "arm_1",
                "target_tool_id": "thyroid_retractor",
            }
        )
    with pytest.raises(ValueError, match="target_side left, right, or both"):
        validate_retraction_command(
            {"command": "adjust_retraction", "target_side": "none", "distance_m": 0.05}
        )
    with pytest.raises(ValueError, match="requires target_side none"):
        validate_retraction_command(
            {"command": "change_tool", "target_side": "left", "distance_m": 0}
        )
    with pytest.raises(ValueError, match="finite"):
        validate_retraction_command(
            {"command": "adjust_retraction", "target_side": "left", "distance_m": float("nan")}
        )


def test_retraction_adjustment_enforces_the_five_centimetre_contract_limit() -> None:
    accepted = validate_retraction_command(
        {
            "command": "adjust_retraction",
            "target_side": "right",
            "distance_m": 0.050,
        }
    )
    assert accepted["distance_m"] == 0.050

    release = validate_retraction_command(
        {
            "command": "adjust_retraction",
            "target_side": "right",
            "distance_m": -0.010,
        }
    )
    assert release["distance_m"] == -0.010

    with pytest.raises(ValueError, match="non-zero"):
        validate_retraction_command(
            {
                "command": "adjust_retraction",
                "target_side": "right",
                "distance_m": 0.0,
            }
        )

    with pytest.raises(ValueError, match=r"at most 0\.050"):
        validate_retraction_command(
            {
                "command": "adjust_retraction",
                "target_side": "right",
                "distance_m": -0.051,
            }
        )


@pytest.mark.parametrize("command", ["suction", "suction_out"])
def test_suction_commands_are_zero_argument_retraction_service_commands(
    command: str,
) -> None:
    assert validate_retraction_command(
        {"command": command, "target_side": "none", "distance_m": 0.0}
    ) == {
        "command": command,
        "target_side": "none",
        "distance_m": 0.0,
    }


def test_bed_robot_status_requires_the_documented_procedure_layout() -> None:
    arms = validate_bed_robot_arm_status(
        "nephrectomy",
        [
            {
                "arm_id": "arm_1",
                "role": "retraction",
                "role_instance_id": "left_malleable",
                "state": "retracting",
                "direct_teach_active": False,
                "reason_code": "",
            },
            {
                "arm_id": "arm_2",
                "role": "retraction",
                "role_instance_id": "right_malleable",
                "state": "direct_teach",
                "direct_teach_active": True,
                "reason_code": "manual_control",
            },
        ],
    )
    assert [arm["role_instance_id"] for arm in arms] == [
        "left_malleable",
        "right_malleable",
    ]

    with pytest.raises(ValueError, match="role_instance_id|layout"):
        validate_bed_robot_arm_status("thyroidectomy", arms)
    with pytest.raises(ValueError, match="role must be retraction"):
        validate_bed_robot_arm_status(
            "thyroidectomy",
            [
                {
                    "arm_id": "arm_1",
                    "role": "suction",
                    "role_instance_id": "army_navy",
                    "state": "standby",
                    "direct_teach_active": False,
                }
            ],
        )


def test_voice_tool_handover_is_deterministic() -> None:
    parsed = parse_voice_command("켈리 주세요", VOICE)
    assert parsed.matched
    assert parsed.operation == "tool_handover"
    assert parsed.payload["instrument_id"] == "Kelly forceps"


def test_voice_ambiguity_never_dispatches() -> None:
    parsed = parse_voice_command("켈리와 보비 주세요", VOICE)
    assert not parsed.matched
    assert parsed.ambiguous


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("직접 교시 시작", "start_direct_teach"),
        ("직접 교시 종료", "finish_direct_teach"),
        ("Retraction 시작", "start_retraction"),
        ("Tool change", "change_tool"),
        ("Retraction 종료", "stop_retraction"),
    ],
)
def test_voice_retraction_service_commands_are_exact(
    text: str, command: str
) -> None:
    parsed = parse_voice_command(text, VOICE)
    assert parsed.matched
    assert parsed.operation == "retraction_command"
    assert parsed.payload == {
        "command": command,
        "target_side": "none",
        "distance_m": 0.0,
    }


@pytest.mark.parametrize("side", ["none", "left", "right", "both"])
def test_finish_direct_teach_accepts_each_target_side(side: str) -> None:
    parsed = validate_retraction_command(
        {
            "command": "finish_direct_teach",
            "target_side": side,
            "distance_m": 0.0,
        }
    )

    assert parsed["target_side"] == side


def test_finish_direct_teach_rejects_a_distance() -> None:
    with pytest.raises(ValueError, match="finish_direct_teach allows"):
        validate_retraction_command(
            {
                "command": "finish_direct_teach",
                "target_side": "left",
                "distance_m": 0.001,
            }
        )


def test_voice_retraction_adjustment_uses_side_and_distance_only() -> None:
    parsed = parse_voice_command("리트랙션 오른쪽 5cm 더", VOICE)
    assert parsed.matched
    assert parsed.operation == "retraction_command"
    assert parsed.payload == {
        "command": "adjust_retraction",
        "target_side": "right",
        "distance_m": 0.05,
    }

    legacy_direction = parse_voice_command("왼쪽 견인기 왼쪽 5밀리 이동", VOICE)
    assert legacy_direction.matched
    assert legacy_direction.payload == {
        "command": "adjust_retraction",
        "target_side": "left",
        "distance_m": 0.005,
    }

    ambiguous_sides = parse_voice_command("리트랙션 왼쪽 오른쪽 1cm 더", VOICE)
    assert not ambiguous_sides.matched
    assert ambiguous_sides.ambiguous
    assert ambiguous_sides.reason == "ambiguous_retraction_target_side"

    legacy_multi = parse_voice_command("리트랙션 좌우로 1센치 더", VOICE)
    assert legacy_multi.matched
    assert legacy_multi.payload == {
        "command": "adjust_retraction",
        "target_side": "both",
        "distance_m": 0.01,
    }

    natural_bilateral = parse_voice_command("양쪽으로 1mm씩 당겨줘", VOICE)
    assert natural_bilateral.matched
    assert natural_bilateral.payload == {
        "command": "adjust_retraction",
        "target_side": "both",
        "distance_m": 0.001,
    }


def test_bed_mounted_suction_and_legacy_release_are_not_commands() -> None:
    suction = parse_voice_command("석션 켜", VOICE)
    assert not suction.matched

    release = parse_voice_command("리트랙터 해제", VOICE)
    assert not release.matched


def test_debug_config_exposes_exact_public_contract() -> None:
    config = load_config(
        Path(__file__).parents[1] / "config" / "integration_debug.yaml"
    )
    assert {(row["topic"], row["type"]) for row in config["inputs"]} == {
        ("/sensors/surgeon/sentence", "std_msgs/msg/String"),
        (
            "/surgery/audio/observed_utterance",
            "surgical_msgs/msg/SpeechUtterance",
        ),
        ("/integration/cv_contract/status", "std_msgs/msg/String"),
        ("/synced/cam_1/status", "std_msgs/msg/String"),
        ("/synced/cam_2/status", "std_msgs/msg/String"),
        ("/synced/cam_3/status", "std_msgs/msg/String"),
        ("/synced/cam_4/status", "std_msgs/msg/String"),
        ("/synced/flir/status", "std_msgs/msg/String"),
    }
    assert {(row["topic"], row["type"], row["qos"]) for row in config["outputs"]} == {
        ("/surgery/context", "surgical_interop_msgs/msg/SurgeryContext", "snapshot"),
        ("/surgery/instruments", "surgical_interop_msgs/msg/InstrumentStateArray", "snapshot"),
        ("/surgery/robots", "surgical_interop_msgs/msg/RobotStateArray", "snapshot"),
        ("/surgery/events", "surgical_interop_msgs/msg/SurgeryEvent", "event"),
        (
            "/surgery/clinical_observations",
            "surgical_interop_msgs/msg/ClinicalObservationArray",
            "snapshot",
        ),
        ("/surgery/health", "surgical_interop_msgs/msg/SurgeryHealth", "snapshot"),
    }


def test_command_payload_must_be_a_json_object() -> None:
    assert decode_payload('{"enabled": true}') == {"enabled": True}
    with pytest.raises(ValueError, match="JSON object"):
        decode_payload("[]")


def test_planner_coexistence_acknowledgement_requires_exact_node_set() -> None:
    blocked = ["tree_executor", "simulation_manager"]
    assert validate_planner_coexistence_acknowledgement(
        {
            "planner_coexistence_confirmed": True,
            "acknowledged_blocked_nodes": [
                "simulation_manager",
                "tree_executor",
                "tree_executor",
            ],
        },
        blocked,
    ) == ["simulation_manager", "tree_executor"]

    with pytest.raises(ValueError, match="partner planner is paused"):
        validate_planner_coexistence_acknowledgement(
            {"acknowledged_blocked_nodes": blocked}, blocked
        )
    with pytest.raises(ValueError, match="node set changed"):
        validate_planner_coexistence_acknowledgement(
            {
                "planner_coexistence_confirmed": True,
                "acknowledged_blocked_nodes": ["tree_executor"],
            },
            blocked,
        )


def test_planner_coexistence_acknowledgement_is_not_needed_without_blockers() -> None:
    assert validate_planner_coexistence_acknowledgement({}, []) == []


def test_manual_writes_fail_closed_while_full_runtime_is_active() -> None:
    reason = manual_write_block_reason(
        armed=False,
        fault_locked=False,
        blocked_nodes=["simulation_manager", "tree_executor"],
        planner_coexistence_allowed=False,
        acknowledged_blocked_nodes=[],
    )
    assert reason == (
        "full Taskplanner nodes are active: simulation_manager, tree_executor"
    )


def test_manual_writes_never_allow_standalone_planner_coexistence() -> None:
    values = {
        "armed": True,
        "fault_locked": False,
        "blocked_nodes": ["simulation_manager", "tree_executor"],
        "planner_coexistence_allowed": True,
    }
    expected = "full Taskplanner nodes are active: simulation_manager, tree_executor"
    assert manual_write_block_reason(
        **values,
        acknowledged_blocked_nodes=["tree_executor"],
    ) == expected
    assert manual_write_block_reason(
        **values,
        acknowledged_blocked_nodes=["tree_executor", "simulation_manager"],
    ) == expected


def test_manual_output_write_requires_arming_when_runtime_is_stopped() -> None:
    assert manual_write_block_reason(
        armed=False,
        fault_locked=False,
        blocked_nodes=[],
        planner_coexistence_allowed=False,
        acknowledged_blocked_nodes=[],
    ) == "manual control is not armed"
    assert manual_write_block_reason(
        armed=False,
        fault_locked=False,
        blocked_nodes=[],
        planner_coexistence_allowed=False,
        acknowledged_blocked_nodes=[],
    ) == "manual control is not armed"


def test_fault_lock_always_blocks_manual_ros_writes() -> None:
    assert manual_write_block_reason(
        armed=True,
        fault_locked=True,
        blocked_nodes=[],
        planner_coexistence_allowed=False,
        acknowledged_blocked_nodes=[],
    ) == "manual control is fault locked"


@pytest.mark.parametrize(
    ("running", "execution_state"),
    [
        (True, "paused"),
        (False, "idle"),
        (False, "halted"),
        (False, "stopped"),
    ],
)
def test_operational_intervention_accepts_paused_or_fully_stopped_idle_state(
    running: bool,
    execution_state: str,
) -> None:
    assert operational_runtime_intervention_block_reason(
        received=True,
        running=running,
        execution_state=execution_state,
        active_robot_task_id="",
        robot_state="idle",
        cleaner_busy=False,
        publisher_trusted=True,
        age_sec=0.1,
        max_age_sec=3.0,
    ) == ""


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"received": False}, "state is unavailable"),
        ({"age_sec": 3.1}, "state is stale"),
        (
            {"running": True, "execution_state": "running"},
            "pause or stop",
        ),
        (
            {"running": False, "execution_state": "paused"},
            "pause state is inconsistent",
        ),
        (
            {"running": True, "execution_state": "idle"},
            "stopped state is inconsistent",
        ),
    ],
)
def test_operational_intervention_gate_fails_closed_with_stable_reason(
    overrides: dict[str, object],
    reason: str,
) -> None:
    values: dict[str, object] = {
        "received": True,
        "running": True,
        "execution_state": "paused",
        "active_robot_task_id": "",
        "robot_state": "idle",
        "cleaner_busy": False,
        "publisher_trusted": True,
        "age_sec": 0.1,
        "max_age_sec": 3.0,
    }
    values.update(overrides)

    assert reason in operational_runtime_intervention_block_reason(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"active_robot_task_id": "task-17"},
        {"robot_state": "moving"},
        {"robot_state": "unknown"},
        {"cleaner_busy": True},
    ],
)
def test_paused_or_stopped_manual_intervention_does_not_recheck_mirrored_resources(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "received": True,
        "running": True,
        "execution_state": "paused",
        "active_robot_task_id": "",
        "robot_state": "idle",
        "cleaner_busy": False,
        "publisher_trusted": True,
        "age_sec": 0.1,
        "max_age_sec": 3.0,
    }
    values.update(overrides)
    assert operational_runtime_intervention_block_reason(**values) == ""


def test_operational_intervention_requires_the_authoritative_state_publisher() -> None:
    assert operational_runtime_intervention_block_reason(
        received=True,
        running=True,
        execution_state="paused",
        active_robot_task_id="",
        robot_state="idle",
        cleaner_busy=False,
        publisher_trusted=False,
        age_sec=0.1,
        max_age_sec=3.0,
    ) == "operational runtime state publisher is not authoritative"


def test_admitted_command_keeps_only_the_paused_or_stopped_lifecycle_gate() -> None:
    assert operational_runtime_intervention_block_reason(
        received=True,
        running=True,
        execution_state="paused",
        active_robot_task_id="debug-task",
        robot_state="moving",
        cleaner_busy=True,
        publisher_trusted=True,
        age_sec=0.1,
        max_age_sec=3.0,
        require_idle_resources=False,
    ) == ""
    assert "pause or stop" in operational_runtime_intervention_block_reason(
        received=True,
        running=True,
        execution_state="running",
        active_robot_task_id="debug-task",
        robot_state="moving",
        cleaner_busy=True,
        publisher_trusted=True,
        age_sec=0.1,
        max_age_sec=3.0,
        require_idle_resources=False,
    )


@pytest.mark.parametrize(
    "execution_state", ["idle", "halted", "stopped", "completed", "terminated"]
)
def test_operational_runtime_accepts_only_fresh_explicit_stopped_states(
    execution_state: str,
) -> None:
    assert operational_runtime_stopped(
        received=True,
        running=False,
        execution_state=execution_state,
        active_robot_task_id="",
        robot_state="idle",
        cleaner_busy=False,
        publisher_trusted=True,
        age_sec=2.9,
        max_age_sec=3.0,
    )


@pytest.mark.parametrize(
    ("received", "running", "execution_state", "age_sec"),
    [
        (False, False, "idle", None),
        (True, True, "idle", 0.1),
        (True, False, "running", 0.1),
        (True, False, "starting", 0.1),
        (True, False, "paused", 0.1),
        (True, False, "stopping", 0.1),
        (True, False, "resetting", 0.1),
        (True, False, "unknown", 0.1),
        (True, False, "idle", 3.1),
    ],
)
def test_operational_runtime_fails_closed_for_active_or_untrusted_state(
    received: bool,
    running: bool,
    execution_state: str,
    age_sec: float | None,
) -> None:
    assert not operational_runtime_stopped(
        received=received,
        running=running,
        execution_state=execution_state,
        active_robot_task_id="",
        robot_state="idle",
        cleaner_busy=False,
        publisher_trusted=True,
        age_sec=age_sec,
        max_age_sec=3.0,
    )


@pytest.mark.parametrize(
    ("active_robot_task_id", "robot_state", "cleaner_busy"),
    [
        ("task-17", "idle", False),
        ("", "moving", False),
        ("", "handover_in_progress", False),
        ("", "idle", True),
    ],
)
def test_operational_runtime_rejects_orphan_robot_activity(
    active_robot_task_id: str,
    robot_state: str,
    cleaner_busy: bool,
) -> None:
    assert not operational_runtime_stopped(
        received=True,
        running=False,
        execution_state="idle",
        active_robot_task_id=active_robot_task_id,
        robot_state=robot_state,
        cleaner_busy=cleaner_busy,
        publisher_trusted=True,
        age_sec=0.1,
        max_age_sec=3.0,
    )


def test_operational_runtime_rejects_untrusted_state_publisher() -> None:
    assert not operational_runtime_stopped(
        received=True,
        running=False,
        execution_state="idle",
        active_robot_task_id="",
        robot_state="idle",
        cleaner_busy=False,
        publisher_trusted=False,
        age_sec=0.1,
        max_age_sec=3.0,
    )


def test_operational_state_requires_one_exact_publisher_identity() -> None:
    assert operational_state_publisher_trusted(
        ["/or_digital_twin"], "/or_digital_twin"
    )
    assert not operational_state_publisher_trusted([], "/or_digital_twin")
    assert not operational_state_publisher_trusted(
        ["/or_digital_twin", "/spoof"], "/or_digital_twin"
    )
    assert not operational_state_publisher_trusted(
        ["/unexpected"], "/or_digital_twin"
    )


def test_measured_rate_uses_recent_window() -> None:
    rate, count = measured_rate([1.0, 2.0, 3.0, 8.0, 9.0], 9.0, 2.0)
    assert count == 2
    assert rate == 1.0


def test_action_watchdog_policy_is_loaded_from_debug_config() -> None:
    config = load_config(
        Path(__file__).parents[1] / "config" / "integration_debug.yaml"
    )
    policy = load_action_watchdog_policy(config)
    assert policy == {
        "goal_response_timeout_sec": 10.0,
        "change_tool_response_timeout_sec": 120.0,
        "feedback_timeout_sec": 30.0,
        "max_duration_sec": 300.0,
        "server_loss_grace_sec": 5.0,
    }
    with pytest.raises(ValueError, match="greater than 0"):
        load_action_watchdog_policy(
            {"action_watchdog": {"feedback_timeout_sec": 0}}
        )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {
                "server_ready": False,
                "server_unavailable_age_sec": 5.1,
            },
            "action_server_unavailable",
        ),
        (
            {"state": "submitting", "elapsed_sec": 10.1},
            "goal_response_timeout",
        ),
        (
            {
                "state": "executing",
                "elapsed_sec": 31.0,
                "last_update_age_sec": 30.1,
            },
            "action_update_timeout",
        ),
        (
            {
                "state": "executing",
                "elapsed_sec": 300.1,
                "last_update_age_sec": 0.1,
            },
            "action_duration_timeout",
        ),
    ],
)
def test_action_watchdog_reports_uncertain_remote_state(
    overrides: dict[str, object], expected: str
) -> None:
    values: dict[str, object] = {
        "terminal": False,
        "recovery_required": False,
        "state": "accepted",
        "route": "tool_handover",
        "elapsed_sec": 1.0,
        "last_update_age_sec": 1.0,
        "server_ready": True,
        "server_unavailable_age_sec": 0.0,
        "policy": load_action_watchdog_policy({}),
    }
    values.update(overrides)
    assert action_watchdog_reason(**values) == expected


def test_action_watchdog_allows_server_loss_grace_and_terminal_states() -> None:
    policy = load_action_watchdog_policy({})
    common = {
        "state": "accepted",
        "route": "tool_handover",
        "elapsed_sec": 2.0,
        "last_update_age_sec": 2.0,
        "server_ready": False,
        "server_unavailable_age_sec": 4.9,
        "policy": policy,
    }
    assert action_watchdog_reason(
        terminal=False, recovery_required=False, **common
    ) == ""
    assert action_watchdog_reason(
        terminal=True,
        recovery_required=False,
        **{**common, "server_unavailable_age_sec": 10.0},
    ) == ""
    assert action_watchdog_reason(
        terminal=False,
        recovery_required=True,
        **{**common, "server_unavailable_age_sec": 10.0},
    ) == ""


def test_action_watchdog_uses_service_reasons_only_for_retraction_service() -> None:
    policy = load_action_watchdog_policy({})
    common = {
        "terminal": False,
        "recovery_required": False,
        "state": "submitting",
        "elapsed_sec": 10.1,
        "last_update_age_sec": 0.1,
        "server_ready": True,
        "server_unavailable_age_sec": 0.0,
        "policy": policy,
    }
    assert action_watchdog_reason(route="retraction_service", **common) == (
        "service_response_timeout"
    )
    assert action_watchdog_reason(route="tool_handover", **common) == (
        "goal_response_timeout"
    )

    feedback_wait = {
        **common,
        "state": "accepted",
        "elapsed_sec": 20.0,
        "last_update_age_sec": 31.0,
    }
    assert action_watchdog_reason(route="retraction_service", **feedback_wait) == ""
    assert action_watchdog_reason(route="tool_handover", **feedback_wait) == (
        "action_update_timeout"
    )


def test_action_client_recovery_requires_exact_command_and_confirmation() -> None:
    command_id = "debug-command-7"
    assert validate_action_recovery_acknowledgement(
        {
            "expected_command_id": command_id,
            "remote_motion_stopped_confirmed": True,
        },
        command_id,
    ) == command_id
    with pytest.raises(ValueError, match="active command changed"):
        validate_action_recovery_acknowledgement(
            {
                "expected_command_id": "debug-command-6",
                "remote_motion_stopped_confirmed": True,
            },
            command_id,
        )
    with pytest.raises(ValueError, match="confirm that remote motion stopped"):
        validate_action_recovery_acknowledgement(
            {"expected_command_id": command_id}, command_id
        )
