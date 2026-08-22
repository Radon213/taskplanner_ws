from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "retraction_control"
    / "command_server_node.py"
).read_text(encoding="utf-8")


def test_ros_contract_uses_single_native_service_and_existing_status_topic():
    assert 'SERVICE_NAME = "/surgery/retraction/command"' in SOURCE
    assert 'STATUS_TOPIC = "/external/bed_robot_arms/status"' in SOURCE
    assert "ExecuteRetractionCommand" in SOURCE
    assert "BedRobotArmStateArray" in SOURCE
    assert "rosbridge" not in SOURCE.casefold()
    assert "roslibpy" not in SOURCE.casefold()


def test_service_callback_only_admits_and_never_calls_physical_executor():
    callback = SOURCE.split("    def _on_command", 1)[1].split("\n    def ", 1)[0]
    assert "self._admission.admit(request)" in callback
    assert ".execute(" not in callback
    assert "move_" not in callback
    assert "jog_" not in callback


def test_hardware_mode_has_three_independent_fail_closed_fences():
    assert 'hardware mode requires explicit allow_motion=true' in SOURCE
    assert 'synthetic_fake profile can never authorize hardware mode' in SOURCE
    assert 'hardware backend injection is not implemented' in SOURCE


def test_process_authority_is_acquired_before_restart_ledger_is_mutated():
    assert SOURCE.index("self._backend.start()") < SOURCE.index("CommandLedger(")


def test_ros_data_directory_override_uses_the_strict_runtime_validator():
    assert "data_directory = validate_data_directory(" in SOURCE


def test_diagnostics_carry_details_missing_from_fixed_public_status_idl():
    for key in (
        '"active_command_id"',
        '"active_operation"',
        '"last_terminal_outcome"',
        '"last_terminal_command_id"',
        '"last_terminal_command_trace_count"',
        '"last_error_code"',
        '"robot_connected"',
        '"sensor_available"',
        '"physical_completion_confirmed"',
        '"execution_evidence"',
    ):
        assert key in SOURCE


def test_shadow_uses_record_only_adapter_and_never_publishes_physical_state():
    assert "ShadowIndyDcp3Adapter" in SOURCE
    assert 'state = "unknown"' in SOURCE
    assert 'return "shadow_record_only"' in SOURCE
    assert "ShadowTraceRepository" in SOURCE


def test_main_guards_each_ros_cleanup_step_against_repeated_sigint():
    main = SOURCE.split("def main", 1)[1]
    assert main.count("except KeyboardInterrupt:") == 4
    assert main.index("executor.shutdown()") < main.index("node.destroy_node()")
    assert main.index("node.destroy_node()") < main.index("rclpy.shutdown()")
