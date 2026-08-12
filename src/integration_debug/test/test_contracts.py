from pathlib import Path

import pytest

from integration_debug.contracts import (
    action_watchdog_reason,
    decode_payload,
    load_action_watchdog_policy,
    load_config,
    measured_rate,
    parse_voice_command,
    validate_action_recovery_acknowledgement,
    validate_planner_coexistence_acknowledgement,
    validate_retraction,
    validate_tool_handover,
)


VOICE = {
    "default_source_location": "tray",
    "default_target_location": "surgeon",
    "aliases": {"켈리": "Kelly forceps", "보비": "Bovie surgical cautery"},
}


def test_validates_only_public_handover_transitions() -> None:
    mapped = validate_tool_handover(
        {
            "instrument_id": "Kelly forceps",
            "source_location": "tray",
            "target_location": "surgeon",
        }
    )
    assert mapped["instrument_instance_id"] == "Kelly forceps#1"
    with pytest.raises(ValueError, match="unsupported"):
        validate_tool_handover(
            {
                "instrument_id": "Kelly forceps",
                "source_location": "surgeon",
                "target_location": "robot",
            }
        )


def test_retraction_is_discrete_and_bounded() -> None:
    mapped = validate_retraction(
        {"operation": "MOVE", "direction": "left", "distance_mm": 5}
    )
    assert mapped["direction"] == "LEFT"
    with pytest.raises(ValueError, match="at most 30"):
        validate_retraction(
            {"operation": "MOVE", "direction": "LEFT", "distance_mm": 31}
        )

    assert validate_retraction({"operation": "RELEASE"}) == {
        "operation": "RELEASE",
        "direction": "",
        "distance_mm": 0.0,
        "end_effector_profile": "",
    }
    changed = validate_retraction(
        {"operation": "CHANGE_END_EFFECTOR", "end_effector_profile": "wide"}
    )
    assert changed["end_effector_profile"] == "wide"


def test_voice_tool_handover_is_deterministic() -> None:
    parsed = parse_voice_command("켈리 주세요", VOICE)
    assert parsed.matched
    assert parsed.operation == "tool_handover"
    assert parsed.payload["instrument_id"] == "Kelly forceps"


def test_voice_ambiguity_never_dispatches() -> None:
    parsed = parse_voice_command("켈리와 보비 주세요", VOICE)
    assert not parsed.matched
    assert parsed.ambiguous


def test_voice_retraction_requires_direction_and_distance() -> None:
    parsed = parse_voice_command("리트랙터 왼쪽 5밀리 이동", VOICE)
    assert parsed.matched
    assert parsed.payload["direction"] == "LEFT"
    assert parsed.payload["distance_mm"] == 5.0
    assert not parse_voice_command("리트랙터 왼쪽 이동", VOICE).matched


def test_voice_suction_and_release_commands_are_explicit() -> None:
    suction = parse_voice_command("석션 켜", VOICE)
    assert suction.matched
    assert suction.operation == "suction"
    assert suction.payload == {"enabled": True}

    release = parse_voice_command("리트랙터 해제", VOICE)
    assert release.matched
    assert release.operation == "retraction"
    assert release.payload["operation"] == "RELEASE"


def test_debug_config_exposes_exact_public_contract() -> None:
    config = load_config(
        Path(__file__).parents[1] / "config" / "integration_debug.yaml"
    )
    assert {(row["topic"], row["type"]) for row in config["inputs"]} == {
        ("/sensors/surgeon/sentence", "std_msgs/msg/String"),
        ("/camera/cam_1/color/image_raw/compressed", "sensor_msgs/msg/CompressedImage"),
        ("/camera/cam_2/color/image_raw/compressed", "sensor_msgs/msg/CompressedImage"),
        ("/camera/cam_3/color/image_raw/compressed", "sensor_msgs/msg/CompressedImage"),
        ("/camera/cam_4/color/image_raw/compressed", "sensor_msgs/msg/CompressedImage"),
        ("/flir_camera/image_color/compressed", "sensor_msgs/msg/CompressedImage"),
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
