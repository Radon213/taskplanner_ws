from retraction_control.diagnostics import ArmStatus, DiagnosticSnapshot, public_arm_state


def test_internal_states_map_to_existing_public_contract():
    assert public_arm_state("IDLE") == "standby"
    assert public_arm_state("DIRECT_TEACHING") == "direct_teach"
    assert public_arm_state("RETRACTING") == "retracting"
    assert public_arm_state("TOOL_CHANGING") == "changing_tool"
    assert public_arm_state("STOPPING") == "moving_to_standby"
    assert public_arm_state("FAULT") == "fault"
    assert public_arm_state("new_state") == "unknown"


def test_direct_teach_flag_is_derived_from_public_state():
    assert ArmStatus("arm_1", "army_navy", "direct_teach").direct_teach_active
    assert not ArmStatus("arm_1", "army_navy", "standby").direct_teach_active


def test_diagnostic_health_fails_closed_on_sensor_or_controller_fault():
    base = dict(
        connected=True,
        internal_state="IDLE",
        active_command_id="",
        operation="",
        fault_code="",
        fault_message="",
        pending_count=0,
        sensor_fresh=True,
        profile_id="fake",
        profile_checksum="abc",
        adapter_mode="fake",
        extras={},
    )
    assert DiagnosticSnapshot(**base).healthy
    assert not DiagnosticSnapshot(**{**base, "sensor_fresh": False}).healthy
    assert not DiagnosticSnapshot(**{**base, "fault_code": "sdk_error"}).healthy

