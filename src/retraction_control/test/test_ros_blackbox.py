"""Loopback-only ROS black-box coverage for the six-command fake lifecycle.

The probes run in fresh subprocesses so DDS discovery, client reconnect, and
server restart are exercised without sharing an rclpy Context with pytest.
No hardware adapter or non-loopback discovery path is enabled.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest


rclpy = pytest.importorskip("rclpy")
diagnostic_msgs = pytest.importorskip("diagnostic_msgs.msg")
interop_msgs = pytest.importorskip("surgical_interop_msgs.msg")
interop_srv = pytest.importorskip("surgical_interop_msgs.srv")

DiagnosticArray = diagnostic_msgs.DiagnosticArray
BedRobotArmStateArray = interop_msgs.BedRobotArmStateArray
ExecuteRetractionCommand = interop_srv.ExecuteRetractionCommand

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = PACKAGE_ROOT / "config"
SERVICE_NAME = "/surgery/retraction/command"
STATUS_TOPIC = "/external/bed_robot_arms/status"
DIAGNOSTICS_TOPIC = "/diagnostics"


def _loopback_environment(domain_id: int) -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "CYCLONEDDS_URI",
        "FASTRTPS_DEFAULT_PROFILES_FILE",
        "FASTDDS_DEFAULT_PROFILES_FILE",
        "ROS_DISCOVERY_SERVER",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "ROS_DOMAIN_ID": str(domain_id),
            "ROS_LOCALHOST_ONLY": "1",
            "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",
            "PYTHONDONTWRITEBYTECODE": "1",
            "RCUTILS_LOGGING_USE_STDOUT": "1",
        }
    )
    return environment


def _spin_until(node, predicate, timeout_sec: float, description: str) -> None:
    deadline = time.monotonic() + timeout_sec
    while not predicate():
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise AssertionError(f"timed out waiting for {description}")
        rclpy.spin_once(node, timeout_sec=min(0.05, remaining))


def _diagnostic_values(message) -> dict[str, str]:
    for status in message.status:
        if status.name == "retraction_control/controller":
            return {item.key: item.value for item in status.values}
    return {}


def _call(client, node, command_id, command, target_side=0, distance_m=0.0):
    request = ExecuteRetractionCommand.Request()
    request.protocol_version = 1
    request.source_id = "taskplanner"
    request.command_id = command_id
    request.command = command
    request.target_side = target_side
    request.distance_m = distance_m
    future = client.call_async(request)
    _spin_until(node, future.done, 8.0, f"Service response for {command_id}")
    response = future.result()
    if response is None:
        raise AssertionError(f"Service returned no response for {command_id}")
    return response


def _run_service_discovery_probe(timeout_sec: float) -> dict[str, object]:
    rclpy.init(args=[])
    node = rclpy.create_node(f"retraction_discovery_probe_{os.getpid()}")
    try:
        client = node.create_client(ExecuteRetractionCommand, SERVICE_NAME)
        found = client.wait_for_service(timeout_sec=timeout_sec)
        return {"service_found": bool(found)}
    finally:
        node.destroy_node()
        rclpy.shutdown()


def _run_six_command_scenario() -> dict[str, object]:
    rclpy.init(args=[])
    node = rclpy.create_node(f"retraction_scenario_probe_{os.getpid()}")
    statuses = []
    diagnostics = []
    status_history: list[tuple[int, int]] = []

    def on_status(message):
        statuses.append(message)
        timestamp_ns = int(message.stamp.sec) * 1_000_000_000
        timestamp_ns += int(message.stamp.nanosec)
        if not status_history or status_history[-1][0] != int(message.revision):
            status_history.append((int(message.revision), timestamp_ns))

    node.create_subscription(
        BedRobotArmStateArray,
        STATUS_TOPIC,
        on_status,
        10,
    )
    node.create_subscription(DiagnosticArray, DIAGNOSTICS_TOPIC, diagnostics.append, 10)
    client = node.create_client(ExecuteRetractionCommand, SERVICE_NAME)

    def latest_values() -> dict[str, str]:
        return _diagnostic_values(diagnostics[-1]) if diagnostics else {}

    def state_is(expected: str) -> bool:
        return bool(
            statuses
            and statuses[-1].arms
            and all(arm.state == expected for arm in statuses[-1].arms)
        )

    def await_terminal(command_id: str, expected_state: str) -> None:
        _spin_until(
            node,
            lambda: (
                latest_values().get("last_terminal_command_id") == command_id
                and latest_values().get("worker_stage") == "completed"
                and latest_values().get("last_terminal_outcome") == "succeeded"
                and state_is(expected_state)
            ),
            8.0,
            f"terminal state for {command_id}",
        )

    def accepted(command_id, command, expected_state, target_side=0, distance_m=0.0):
        response = _call(
            client,
            node,
            command_id,
            command,
            target_side=target_side,
            distance_m=distance_m,
        )
        assert response.request_accepted
        assert response.result_code == 0
        assert response.command_id == command_id
        assert response.message == "request_accepted_for_execution"
        await_terminal(command_id, expected_state)
        return response

    try:
        assert client.wait_for_service(timeout_sec=8.0)
        _spin_until(
            node,
            lambda: bool(statuses and diagnostics),
            8.0,
            "initial status and diagnostics",
        )
        assert state_is("standby")

        accepted("ros-teach-start", 1, "direct_teach")
        trace_before_duplicate = int(
            latest_values()["last_terminal_command_trace_count"]
        )

        duplicate = _call(client, node, "ros-teach-start", 1)
        assert duplicate.request_accepted
        assert duplicate.result_code == 0
        assert duplicate.message.startswith("duplicate_no_reexecution:")

        conflict = _call(client, node, "ros-teach-start", 2)
        assert not conflict.request_accepted
        assert conflict.result_code == 2
        assert conflict.message == "command_id_reused_with_different_payload"

        end_duplicate_window = time.monotonic() + 0.25
        while time.monotonic() < end_duplicate_window:
            rclpy.spin_once(node, timeout_sec=0.025)
        assert (
            int(latest_values()["last_terminal_command_trace_count"])
            == trace_before_duplicate
        )

        accepted("ros-teach-finish", 2, "standby")
        accepted("ros-retraction-start", 3, "retracting")
        accepted(
            "ros-adjust-left",
            4,
            "retracting",
            target_side=1,
            distance_m=0.001,
        )
        assert latest_values()["last_affected_arm_id"] == "arm_1"
        accepted("ros-change-tool", 5, "retracting")
        accepted("ros-stop", 6, "standby")

        latest_status = statuses[-1]
        latest_diagnostic = latest_values()
        assert latest_status.procedure_type == "nephrectomy"
        assert {(arm.arm_id, arm.role_instance_id) for arm in latest_status.arms} == {
            ("arm_1", "left_malleable"),
            ("arm_2", "right_malleable"),
        }
        assert latest_diagnostic["adapter_mode"] == "fake"
        assert latest_diagnostic["execution_evidence"] == "synthetic"
        assert latest_diagnostic["physical_completion_confirmed"] == "False"
        assert len(status_history) >= 6
        assert all(
            later[0] > earlier[0] and later[1] > earlier[1]
            for earlier, later in zip(status_history, status_history[1:])
        )
        return {
            "terminal_command_id": latest_diagnostic["last_terminal_command_id"],
            "status_revision": int(latest_status.revision),
            "status_samples": len(status_history),
            "trace_call_count": int(latest_diagnostic["trace_call_count"]),
        }
    finally:
        node.destroy_node()
        rclpy.shutdown()


def _run_restart_duplicate_probe() -> dict[str, object]:
    rclpy.init(args=[])
    node = rclpy.create_node(f"retraction_restart_probe_{os.getpid()}")
    statuses = []
    diagnostics = []
    node.create_subscription(BedRobotArmStateArray, STATUS_TOPIC, statuses.append, 10)
    node.create_subscription(DiagnosticArray, DIAGNOSTICS_TOPIC, diagnostics.append, 10)
    client = node.create_client(ExecuteRetractionCommand, SERVICE_NAME)
    try:
        assert client.wait_for_service(timeout_sec=8.0)
        _spin_until(
            node,
            lambda: bool(statuses and diagnostics),
            8.0,
            "restart status and diagnostics",
        )
        before = int(
            _diagnostic_values(diagnostics[-1])[
                "last_terminal_command_trace_count"
            ]
        )
        response = _call(client, node, "ros-stop", 6)
        assert response.request_accepted
        assert response.result_code == 0
        assert response.message.startswith("duplicate_no_reexecution:")

        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.025)
        after = int(
            _diagnostic_values(diagnostics[-1])[
                "last_terminal_command_trace_count"
            ]
        )
        assert before == after
        assert statuses[-1].arms
        assert all(arm.state == "standby" for arm in statuses[-1].arms)
        return {"trace_before": before, "trace_after": after}
    finally:
        node.destroy_node()
        rclpy.shutdown()


def _run_shadow_probe() -> dict[str, object]:
    rclpy.init(args=[])
    node = rclpy.create_node(f"retraction_shadow_probe_{os.getpid()}")
    statuses = []
    diagnostics = []
    node.create_subscription(BedRobotArmStateArray, STATUS_TOPIC, statuses.append, 10)
    node.create_subscription(DiagnosticArray, DIAGNOSTICS_TOPIC, diagnostics.append, 10)
    client = node.create_client(ExecuteRetractionCommand, SERVICE_NAME)

    def values() -> dict[str, str]:
        return _diagnostic_values(diagnostics[-1]) if diagnostics else {}

    try:
        assert client.wait_for_service(timeout_sec=8.0)
        response = _call(client, node, "ros-shadow-teach", 1)
        assert response.request_accepted
        _spin_until(
            node,
            lambda: (
                bool(statuses and diagnostics)
                and values().get("last_terminal_command_id") == "ros-shadow-teach"
                and values().get("worker_stage") == "completed"
            ),
            8.0,
            "shadow terminal evidence",
        )
        assert statuses[-1].arms
        assert all(arm.state == "unknown" for arm in statuses[-1].arms)
        assert all(
            arm.reason_code == "shadow_record_only" for arm in statuses[-1].arms
        )
        assert values()["execution_evidence"] == "record_only"
        assert values()["physical_completion_confirmed"] == "False"
        assert values()["shadow_trace_error"] == ""
        return {
            "command_trace_count": int(
                values()["last_terminal_command_trace_count"]
            )
        }
    finally:
        node.destroy_node()
        rclpy.shutdown()


def _probe_main() -> int:
    action = sys.argv[1]
    if action == "discover":
        result = _run_service_discovery_probe(float(sys.argv[2]))
    elif action == "scenario":
        result = _run_six_command_scenario()
    elif action == "restart-duplicate":
        result = _run_restart_duplicate_probe()
    elif action == "shadow":
        result = _run_shadow_probe()
    else:
        raise ValueError(f"unknown probe action: {action}")
    print(json.dumps(result, sort_keys=True))
    return 0


def _run_probe(action: str, environment, *arguments: str, timeout=45.0):
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), action, *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"ROS probe {action!r} failed ({completed.returncode})\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if not lines:
        raise AssertionError(f"ROS probe {action!r} returned no JSON")
    return json.loads(lines[-1])


def _start_server(
    data_directory,
    domain_id,
    expected_domain_id,
    log_path,
    adapter_mode="fake",
):
    environment = _loopback_environment(domain_id)
    log_handle = log_path.open("w", encoding="utf-8")
    command = [
        sys.executable,
        "-c",
        "from retraction_control.command_server_node import main; main()",
        "--ros-args",
        "-p",
        f"runtime_config_path:={CONFIG_ROOT / 'logging.yaml'}",
        "-p",
        f"profile_path:={CONFIG_ROOT / 'fake.yaml'}",
        "-p",
        f"data_directory:={data_directory}",
        "-p",
        f"adapter_mode:={adapter_mode}",
        "-p",
        f"expected_ros_domain_id:={expected_domain_id}",
        "-p",
        "status_period_sec:=0.05",
        "-p",
        "diagnostics_period_sec:=0.05",
        "-p",
        "shutdown_timeout_sec:=3.0",
    ]
    process = subprocess.Popen(
        command,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
    return process, log_handle, environment


def _stop_server(process, log_handle, log_path, expect_success=True):
    try:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        return_code = process.wait(timeout=8.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        return_code = process.wait(timeout=3.0)
        raise AssertionError(f"ROS server did not stop cleanly; see {log_path}")
    finally:
        log_handle.close()
    if expect_success and return_code != 0:
        log = log_path.read_text(encoding="utf-8", errors="replace")
        raise AssertionError(f"ROS server exited {return_code}\n{log}")
    return return_code


def test_loopback_ros_six_commands_domain_isolation_and_restart(tmp_path):
    domain_id = 150 + (os.getpid() % 60)
    data_directory = (tmp_path / "runtime-data").resolve()
    data_directory.mkdir()

    mismatch_log = tmp_path / "domain-mismatch.log"
    mismatch, mismatch_handle, _ = _start_server(
        data_directory,
        domain_id,
        domain_id + 1,
        mismatch_log,
    )
    try:
        mismatch_return_code = mismatch.wait(timeout=8.0)
    except subprocess.TimeoutExpired:
        mismatch.terminate()
        mismatch.wait(timeout=3.0)
        raise AssertionError("domain-mismatched server did not fail closed")
    finally:
        mismatch_handle.close()
    assert mismatch_return_code != 0
    assert "ROS_DOMAIN_ID mismatch" in mismatch_log.read_text(encoding="utf-8")

    first_log = tmp_path / "server-first.log"
    first, first_handle, environment = _start_server(
        data_directory,
        domain_id,
        domain_id,
        first_log,
    )
    try:
        discovery = _run_probe("discover", environment, "8.0")
        if not discovery["service_found"]:
            raise AssertionError(first_log.read_text(encoding="utf-8", errors="replace"))

        contender_log = tmp_path / "server-contender.log"
        contender, contender_handle, _ = _start_server(
            data_directory,
            domain_id,
            domain_id,
            contender_log,
        )
        try:
            contender_return_code = contender.wait(timeout=8.0)
        except subprocess.TimeoutExpired:
            contender.terminate()
            contender.wait(timeout=3.0)
            raise AssertionError("second controller process was not rejected")
        finally:
            contender_handle.close()
        assert contender_return_code != 0
        assert "hardware authority is already owned" in contender_log.read_text(
            encoding="utf-8"
        )
        assert _run_probe("discover", environment, "2.0")["service_found"]

        other_domain = _loopback_environment(domain_id + 1)
        assert not _run_probe("discover", other_domain, "0.75")["service_found"]
        scenario = _run_probe("scenario", environment)
        assert scenario["terminal_command_id"] == "ros-stop"
        assert scenario["trace_call_count"] > 0
    finally:
        _stop_server(first, first_handle, first_log)

    ledger_path = data_directory / "command_ledger.sqlite3"
    with sqlite3.connect(ledger_path) as connection:
        records = connection.execute(
            """
            SELECT command_id, stage, admission_accepted
              FROM commands ORDER BY created_ns
            """
        ).fetchall()
    assert [record[0] for record in records] == [
        "ros-teach-start",
        "ros-teach-finish",
        "ros-retraction-start",
        "ros-adjust-left",
        "ros-change-tool",
        "ros-stop",
    ]
    assert all(record[1:] == ("completed", 1) for record in records)

    restart_log = tmp_path / "server-restart.log"
    restart, restart_handle, restart_environment = _start_server(
        data_directory,
        domain_id,
        domain_id,
        restart_log,
    )
    try:
        restarted = _run_probe("restart-duplicate", restart_environment)
        assert restarted["trace_before"] == restarted["trace_after"]
    finally:
        _stop_server(restart, restart_handle, restart_log)

    shadow_directory = (tmp_path / "shadow-data").resolve()
    shadow_directory.mkdir()
    shadow_log = tmp_path / "server-shadow.log"
    shadow, shadow_handle, shadow_environment = _start_server(
        shadow_directory,
        domain_id,
        domain_id,
        shadow_log,
        adapter_mode="shadow",
    )
    try:
        shadow_result = _run_probe("shadow", shadow_environment)
        assert shadow_result["command_trace_count"] > 0
    finally:
        _stop_server(shadow, shadow_handle, shadow_log)

    artifacts = tuple((shadow_directory / "shadow_traces").glob("command-*.json"))
    assert len(artifacts) == 1
    artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert artifact["command_id"] == "ros-shadow-teach"
    assert artifact["evidence_level"] == "record_only"
    assert artifact["physical_motion_executed"] is False
    assert artifact["calls"]
    assert all(call["command_id"] == "ros-shadow-teach" for call in artifact["calls"])


if __name__ == "__main__":
    raise SystemExit(_probe_main())
