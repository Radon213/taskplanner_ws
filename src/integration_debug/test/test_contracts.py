from pathlib import Path

import pytest

from integration_debug.contracts import (
    decode_payload,
    load_config,
    measured_rate,
    parse_voice_command,
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
