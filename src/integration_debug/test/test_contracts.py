from pathlib import Path

import pytest

from integration_debug.contracts import (
    decode_payload,
    load_config,
    measured_rate,
    parse_voice_command,
    validate_bed_robot_arm_status,
    validate_retraction_adjustment,
    validate_tool_change,
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


def test_retraction_adjustment_matches_document_contract() -> None:
    mapped = validate_retraction_adjustment(
        {
            "adjustment_mode": "single",
            "target_retractor_id": "left_malleable",
            "direction_frame": "surgeon_view",
            "direction": "left",
            "axis": "none",
            "distance_mm": 5,
        }
    )
    assert mapped["direction"] == "left"
    with pytest.raises(ValueError, match="at most 30"):
        validate_retraction_adjustment(
            {**mapped, "distance_mm": 31}
        )
    multi = validate_retraction_adjustment(
        {
            "adjustment_mode": "multi",
            "target_retractor_id": "both_malleable",
            "direction_frame": "surgeon_view",
            "direction": "none",
            "axis": "left_right",
            "distance_mm": 3,
        }
    )
    assert multi["axis"] == "left_right"


def test_tool_change_accepts_only_document_ids() -> None:
    assert validate_tool_change(
        {"arm_id": "arm_1", "target_tool_id": "thyroid_retractor"}
    ) == {"arm_id": "arm_1", "target_tool_id": "thyroid_retractor"}
    with pytest.raises(ValueError, match="arm_id"):
        validate_tool_change(
            {"arm_id": "suction_arm", "target_tool_id": "thyroid_retractor"}
        )


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


def test_voice_retraction_requires_direction_and_distance() -> None:
    parsed = parse_voice_command("왼쪽 견인기 왼쪽 5밀리 이동", VOICE)
    assert parsed.matched
    assert parsed.operation == "retraction_adjustment"
    assert parsed.payload["direction"] == "left"
    assert parsed.payload["target_retractor_id"] == "left_malleable"
    assert parsed.payload["distance_mm"] == 5.0
    assert not parse_voice_command("리트랙터 왼쪽 이동", VOICE).matched

    document_example = parse_voice_command(
        "왼쪽 말레어블을 오른쪽으로 1센치 더 당겨주세요",
        VOICE,
    )
    assert document_example.matched
    assert document_example.payload["target_retractor_id"] == "left_malleable"
    assert document_example.payload["direction"] == "right"
    assert document_example.payload["distance_mm"] == 10.0


def test_voice_multi_retraction_uses_axis_not_conflicting_directions() -> None:
    parsed = parse_voice_command("좌우로 1센치 더 당겨주세요", VOICE)
    assert parsed.matched
    assert parsed.operation == "retraction_adjustment"
    assert parsed.payload == {
        "adjustment_mode": "multi",
        "target_retractor_id": "both_malleable",
        "direction_frame": "surgeon_view",
        "direction": "none",
        "axis": "left_right",
        "distance_mm": 10.0,
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
        ("/surgery/images/flir/compressed", "sensor_msgs/msg/CompressedImage"),
        ("/surgery/images/cam4/compressed", "sensor_msgs/msg/CompressedImage"),
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


def test_measured_rate_uses_recent_window() -> None:
    rate, count = measured_rate([1.0, 2.0, 3.0, 8.0, 9.0], 9.0, 2.0)
    assert count == 2
    assert rate == 1.0
