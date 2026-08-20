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


def test_diagnostics_carry_details_missing_from_fixed_public_status_idl():
    for key in (
        '"active_command_id"',
        '"active_operation"',
        '"last_terminal_outcome"',
        '"last_error_code"',
        '"robot_connected"',
        '"sensor_available"',
    ):
        assert key in SOURCE


def test_main_guards_each_ros_cleanup_step_against_repeated_sigint():
    main = SOURCE.split("def main", 1)[1]
    assert main.count("except KeyboardInterrupt:") == 4
    assert main.index("executor.shutdown()") < main.index("node.destroy_node()")
    assert main.index("node.destroy_node()") < main.index("rclpy.shutdown()")
