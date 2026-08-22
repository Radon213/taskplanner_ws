#!/usr/bin/env python3
"""Contract tests for the allowlisted dashboard runtime-mode control API."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "taskplanner_runtime_control.py"
SPEC = importlib.util.spec_from_file_location("taskplanner_runtime_control", MODULE_PATH)
assert SPEC and SPEC.loader
runtime_control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime_control
SPEC.loader.exec_module(runtime_control)


class BlockingProcess:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.return_code = 0
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if not self.done.wait(timeout=timeout):
            raise runtime_control.subprocess.TimeoutExpired("taskplanner", timeout)
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.done.set()

    def kill(self) -> None:
        self.killed = True
        self.done.set()


class RuntimeControlApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.process = BlockingProcess()
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

        def popen_factory(command: list[str], **kwargs: object) -> BlockingProcess:
            self.commands.append(command)
            self.environments.append(dict(kwargs["env"]))  # type: ignore[arg-type]
            return self.process

        self.controller = runtime_control.RuntimeController(
            root=root,
            state_file=root / "active-runtime-mode.json",
            launcher=root / "scripts" / "taskplanner",
            launcher_log_file=root / "runtime-control-launch.log",
            popen_factory=popen_factory,
            mode_running_probe=lambda _mode: True,
            transition_interlock_probe=lambda _mode: True,
            running_modes_probe=lambda: set(),
        )
        self.token = "t" * 48
        self.server = runtime_control.create_server("127.0.0.1", 0, self.controller, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.process.done.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def write_active_mode(self, mode: str) -> None:
        self.controller._state_file.write_text(json.dumps({"mode": mode}), encoding="utf-8")

    def request(self, method: str, path: str, payload: object | None = None, token: bool = True):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if token:
            headers[runtime_control.TOKEN_HEADER] = self.token
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        return urlopen(request, timeout=2)

    def test_status_requires_token(self) -> None:
        with self.assertRaises(HTTPError) as context:
            self.request("GET", "/v1/runtime/status", token=False)
        self.assertEqual(context.exception.code, 401)
        context.exception.close()

    def test_transition_is_allowlisted_and_serialized(self) -> None:
        with self.assertRaises(HTTPError) as context:
            self.request("POST", "/v1/runtime/transition", {"mode": "live; id"})
        self.assertEqual(context.exception.code, 400)
        context.exception.close()
        self.assertEqual(self.commands, [])

        with self.request("POST", "/v1/runtime/transition", {"mode": "replay"}) as response:
            self.assertEqual(response.status, 202)
            payload = json.loads(response.read())
        self.assertEqual(payload["phase"], "starting")
        self.assertEqual(
            self.commands,
            [[str(Path(self.tempdir.name) / "scripts" / "taskplanner"), "up", "replay", "--ensure-build"]],
        )
        self.assertEqual(
            self.environments[0]["TASKPLANNER_RUNTIME_EXPECTED_ACTIVE_MODE"], ""
        )
        self.assertEqual(
            self.environments[0]["TASKPLANNER_RUNTIME_REQUIRE_STOPPED"], "1"
        )

        with self.assertRaises(HTTPError) as context:
            self.request("POST", "/v1/runtime/transition", {"mode": "live"})
        self.assertEqual(context.exception.code, 409)
        context.exception.close()

        self.write_active_mode("replay")
        self.process.done.set()
        deadline = time.monotonic() + 2
        while self.controller.snapshot().phase == "starting" and time.monotonic() < deadline:
            time.sleep(0.01)
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot.phase, "idle")
        self.assertEqual(snapshot.active_mode, "replay")
        self.assertIsNone(snapshot.requested_mode)

    def test_transition_rejects_extra_fields(self) -> None:
        with self.assertRaises(HTTPError) as context:
            self.request("POST", "/v1/runtime/transition", {"mode": "live", "extra": "nope"})
        self.assertEqual(context.exception.code, 400)
        context.exception.close()
        self.assertEqual(self.commands, [])

    def test_debug_transition_carries_the_reviewed_replace_flag(self) -> None:
        accepted, snapshot = self.controller.start_transition("debug")
        self.assertTrue(accepted)
        self.assertEqual(snapshot.phase, "starting")
        self.assertEqual(
            self.commands,
            [[
                str(Path(self.tempdir.name) / "scripts" / "taskplanner"),
                "up",
                "debug",
                "--ensure-build",
                "--replace-active",
            ]],
        )
        self.process.done.set()

    def test_failed_transition_preserves_launcher_preserved_active_mode(self) -> None:
        self.write_active_mode("replay")
        self.process.return_code = 2
        accepted, _snapshot = self.controller.start_transition("live")
        self.assertTrue(accepted)
        self.process.done.set()
        deadline = time.monotonic() + 2
        while self.controller.snapshot().phase == "starting" and time.monotonic() < deadline:
            time.sleep(0.01)
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot.phase, "failed")
        self.assertEqual(snapshot.active_mode, "replay")
        self.assertTrue(snapshot.retryable)
        self.assertTrue(self.controller._state_file.exists())

    def test_failed_transition_stays_unknown_after_launcher_clears_marker(self) -> None:
        self.write_active_mode("replay")
        self.process.return_code = 2
        accepted, _snapshot = self.controller.start_transition("live")
        self.assertTrue(accepted)
        self.controller._state_file.unlink()
        self.process.done.set()
        deadline = time.monotonic() + 2
        while self.controller.snapshot().phase == "starting" and time.monotonic() < deadline:
            time.sleep(0.01)
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot.phase, "failed")
        self.assertIsNone(snapshot.active_mode)
        self.assertTrue(snapshot.retryable)
        self.assertFalse(self.controller._state_file.exists())

    def test_transition_timeout_terminates_launcher_and_unlocks_retry(self) -> None:
        root = Path(self.tempdir.name) / "timeout"
        process = BlockingProcess()
        controller = runtime_control.RuntimeController(
            root=root,
            state_file=root / "active-runtime-mode.json",
            launcher=root / "scripts" / "taskplanner",
            launcher_log_file=root / "runtime-control-launch.log",
            popen_factory=lambda *_args, **_kwargs: process,
            transition_timeout_sec=0.05,
            running_modes_probe=lambda: set(),
        )
        accepted, _snapshot = controller.start_transition("replay")
        self.assertTrue(accepted)
        deadline = time.monotonic() + 2
        while controller.snapshot().phase == "starting" and time.monotonic() < deadline:
            time.sleep(0.01)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.phase, "failed")
        self.assertIsNone(snapshot.active_mode)
        self.assertIn("timed out", snapshot.message)
        self.assertTrue(process.terminated)

        accepted, _snapshot = controller.start_transition("live")
        self.assertTrue(accepted)

    def test_status_invalidates_marker_when_required_container_stops(self) -> None:
        root = Path(self.tempdir.name) / "reconcile"
        root.mkdir()
        state_file = root / "active-runtime-mode.json"
        state_file.write_text('{"mode":"replay"}', encoding="utf-8")
        controller = runtime_control.RuntimeController(
            root=root,
            state_file=state_file,
            launcher=root / "scripts" / "taskplanner",
            mode_running_probe=lambda _mode: False,
            active_probe_ttl_sec=0,
            active_probe_failure_threshold=2,
        )
        first = controller.snapshot()
        self.assertEqual(first.phase, "idle")
        self.assertEqual(first.active_mode, "replay")
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.phase, "failed")
        self.assertIsNone(snapshot.active_mode)
        self.assertEqual(snapshot.requested_mode, "replay")
        self.assertTrue(snapshot.retryable)
        self.assertFalse(state_file.exists())

    def test_transition_rejects_running_paused_and_unknown_active_state(self) -> None:
        self.write_active_mode("replay")
        for probe_result in (False, None):
            with self.subTest(probe_result=probe_result):
                self.controller._transition_interlock_probe = (
                    lambda _mode, result=probe_result: result
                )
                accepted, snapshot = self.controller.start_transition("debug")
                self.assertFalse(accepted)
                self.assertEqual(snapshot.phase, "idle")
                self.assertEqual(snapshot.active_mode, "replay")
                self.assertEqual(self.commands, [])

    def test_http_transition_returns_conflict_for_unsafe_active_state(self) -> None:
        self.write_active_mode("replay")
        for probe_result in (False, None):
            with self.subTest(probe_result=probe_result):
                self.controller._transition_interlock_probe = (
                    lambda _mode, result=probe_result: result
                )
                with self.assertRaises(HTTPError) as context:
                    self.request("POST", "/v1/runtime/transition", {"mode": "debug"})
                self.assertEqual(context.exception.code, 409)
                context.exception.close()
                self.assertEqual(self.commands, [])

    def test_http_transition_accepts_fresh_stopped_active_state(self) -> None:
        self.write_active_mode("replay")
        self.controller._transition_interlock_probe = lambda _mode: True
        with self.request(
            "POST", "/v1/runtime/transition", {"mode": "debug"}
        ) as response:
            self.assertEqual(response.status, 202)
        self.assertEqual(self.commands[0][-1], "--replace-active")
        self.assertEqual(
            self.environments[0]["TASKPLANNER_RUNTIME_EXPECTED_ACTIVE_MODE"],
            "replay",
        )

    def test_transition_accepts_fresh_stopped_active_state(self) -> None:
        self.write_active_mode("replay")
        self.controller._transition_interlock_probe = lambda _mode: True
        accepted, snapshot = self.controller.start_transition("debug")
        self.assertTrue(accepted)
        self.assertEqual(snapshot.phase, "starting")
        self.assertEqual(self.commands[0][-1], "--replace-active")

    def test_missing_marker_still_interlocks_running_core(self) -> None:
        self.controller._running_modes_probe = lambda: {"replay"}
        self.controller._transition_interlock_probe = lambda _mode: False
        accepted, snapshot = self.controller.start_transition("debug")
        self.assertFalse(accepted)
        self.assertEqual(snapshot.phase, "idle")
        self.assertEqual(self.commands, [])

        self.controller._transition_interlock_probe = lambda _mode: True
        accepted, snapshot = self.controller.start_transition("debug")
        self.assertTrue(accepted)
        self.assertEqual(snapshot.phase, "starting")

    def test_missing_marker_rejects_ambiguous_or_unknown_core_detection(self) -> None:
        for candidates in (None, {"live", "replay"}):
            with self.subTest(candidates=candidates):
                self.controller._running_modes_probe = lambda value=candidates: value
                accepted, snapshot = self.controller.start_transition("debug")
                self.assertFalse(accepted)
                self.assertEqual(snapshot.phase, "idle")
                self.assertEqual(self.commands, [])


class RuntimeStateInterlockTests(unittest.TestCase):
    def test_final_gate_allows_only_same_freshly_stopped_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            state_file = root / "active-runtime-mode.json"
            state_file.write_text('{"mode":"replay"}', encoding="utf-8")
            safe, _reason = runtime_control.final_transition_interlock_is_safe(
                root,
                state_file,
                "replay",
                running_modes_probe=lambda: {"replay"},
                inactive_probe=lambda _mode: True,
            )
            self.assertTrue(safe)

            for inactive in (False, None):
                with self.subTest(inactive=inactive):
                    safe, _reason = runtime_control.final_transition_interlock_is_safe(
                        root,
                        state_file,
                        "replay",
                        running_modes_probe=lambda: {"replay"},
                        inactive_probe=lambda _mode, value=inactive: value,
                    )
                    self.assertFalse(safe)

    def test_final_gate_detects_runtime_appearing_after_empty_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            state_file = root / "active-runtime-mode.json"
            safe, _reason = runtime_control.final_transition_interlock_is_safe(
                root,
                state_file,
                None,
                running_modes_probe=lambda: set(),
            )
            self.assertTrue(safe)
            safe, _reason = runtime_control.final_transition_interlock_is_safe(
                root,
                state_file,
                None,
                running_modes_probe=lambda: {"replay"},
            )
            self.assertFalse(safe)

    def test_final_gate_treats_live_and_llm_as_shared_core(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            state_file = root / "active-runtime-mode.json"
            state_file.write_text('{"mode":"llm-surgeon"}', encoding="utf-8")
            safe, _reason = runtime_control.final_transition_interlock_is_safe(
                root,
                state_file,
                "llm-surgeon",
                running_modes_probe=lambda: {"live"},
                inactive_probe=lambda mode: mode == "llm-surgeon",
                reservation_probe=lambda mode: mode == "llm-surgeon",
            )
            self.assertTrue(safe)

    def test_final_gate_requires_atomic_operational_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            state_file = root / "active-runtime-mode.json"
            state_file.write_text('{"mode":"live"}', encoding="utf-8")
            for reserved in (False, None):
                with self.subTest(reserved=reserved):
                    safe, reason = runtime_control.final_transition_interlock_is_safe(
                        root,
                        state_file,
                        "live",
                        running_modes_probe=lambda: {"live"},
                        inactive_probe=lambda _mode: True,
                        reservation_probe=lambda _mode, value=reserved: value,
                    )
                    self.assertFalse(safe)
                    self.assertIn("could not be reserved", reason)

            safe, reason = runtime_control.final_transition_interlock_is_safe(
                root,
                state_file,
                "live",
                running_modes_probe=lambda: {"live"},
                inactive_probe=lambda _mode: True,
                reservation_probe=lambda _mode: True,
            )
            self.assertTrue(safe)
            self.assertIn("transition-reserved", reason)

    def test_final_operational_gate_calls_only_reservation_and_preserves_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            state_file = root / "active-runtime-mode.json"
            state_file.write_text('{"mode":"live"}', encoding="utf-8")
            calls: list[str] = []

            safe, _reason = runtime_control.final_transition_interlock_is_safe(
                root,
                state_file,
                "live",
                running_modes_probe=lambda: {"live"},
                inactive_probe=lambda _mode: calls.append("inactive") or True,
                reservation_probe=lambda _mode: calls.append("reserve") or False,
            )

            self.assertFalse(safe)
            self.assertEqual(calls, ["reserve"])
            self.assertEqual(
                json.loads(state_file.read_text(encoding="utf-8")),
                {"mode": "live"},
            )

    def test_final_gate_rejects_marker_or_candidate_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            state_file = root / "active-runtime-mode.json"
            state_file.write_text('{"mode":"live"}', encoding="utf-8")
            safe, _reason = runtime_control.final_transition_interlock_is_safe(
                root,
                state_file,
                "replay",
                running_modes_probe=lambda: {"replay"},
                inactive_probe=lambda _mode: True,
            )
            self.assertFalse(safe)

    def test_live_state_requires_consistent_inactive_fields(self) -> None:
        self.assertTrue(
            runtime_control.mode_state_is_inactive(
                "live", {"running": False, "execution_state": "idle"}
            )
        )
        for payload in (
            {"running": True, "execution_state": "running"},
            {"running": False, "execution_state": "running"},
            {"running": False, "execution_state": "starting"},
            {"execution_state": "idle"},
        ):
            self.assertFalse(runtime_control.mode_state_is_inactive("live", payload))

    def test_operational_probe_rejects_early_halted_while_termination_pending(self) -> None:
        service_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "response:\nstd_srvs.srv.Trigger_Response("
                "success=False, message='simulation operation is still pending: stop')\n"
            ),
            stderr="",
        )
        with patch.object(
            runtime_control,
            "_running_mode_container_id",
            return_value="runtime-container",
        ), patch.object(
            runtime_control.subprocess,
            "run",
            return_value=service_result,
        ):
            self.assertFalse(
                runtime_control.probe_mode_inactive(Path("/workspace"), "live")
            )

    def test_operational_probe_allows_only_manager_confirmed_settled_state(self) -> None:
        service_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "response:\nstd_srvs.srv.Trigger_Response("
                "success=True, message='"
                f"{runtime_control.TRANSITION_PROTOCOL_MARKER} "
                "transition ready; executor=terminated')\n"
            ),
            stderr="",
        )
        with patch.object(
            runtime_control,
            "_running_mode_container_id",
            return_value="runtime-container",
        ), patch.object(
            runtime_control.subprocess,
            "run",
            return_value=service_result,
        ):
            self.assertTrue(
                runtime_control.probe_mode_inactive(
                    Path("/workspace"), "llm-surgeon"
                )
            )

    def test_operational_reservation_uses_dedicated_trigger(self) -> None:
        service_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "response:\nstd_srvs.srv.Trigger_Response(success=True, message='"
                f"{runtime_control.TRANSITION_PROTOCOL_MARKER} reserved')\n"
            ),
            stderr="",
        )
        with patch.object(
            runtime_control,
            "_running_mode_container_id",
            return_value="runtime-container",
        ), patch.object(
            runtime_control.subprocess,
            "run",
            return_value=service_result,
        ) as run:
            self.assertTrue(
                runtime_control.reserve_mode_transition(Path("/workspace"), "live")
            )
        self.assertIn(
            runtime_control.TRANSITION_RESERVE_SERVICE,
            run.call_args.args[0],
        )
        command = run.call_args.args[0]
        self.assertNotIn("service type", command[5])
        self.assertIn("timeout 4 ros2 service call", command[5])
        self.assertEqual(run.call_args.kwargs["timeout"], 6.0)

    def test_operational_trigger_fails_closed_on_protocol_and_transport_errors(self) -> None:
        cases = (
            subprocess.CompletedProcess([], 0, "success: true\nmessage: old contract\n", ""),
            subprocess.CompletedProcess(
                [],
                0,
                (
                    f"{runtime_control.TRANSITION_PROTOCOL_MARKER}\n"
                    "success: true\nmessage: old contract\n"
                ),
                "",
            ),
            subprocess.CompletedProcess(
                [],
                0,
                f"message: {runtime_control.TRANSITION_PROTOCOL_MARKER}\n",
                "",
            ),
            subprocess.CompletedProcess(
                [],
                0,
                f"success: false\nmessage: {runtime_control.TRANSITION_PROTOCOL_MARKER}\n",
                "",
            ),
            subprocess.CompletedProcess([], 2, "", "wrong or missing service type"),
        )
        for result in cases:
            with self.subTest(result=result):
                with patch.object(
                    runtime_control.subprocess, "run", return_value=result
                ):
                    self.assertIsNot(
                        runtime_control._call_operational_trigger(
                            "runtime-container", runtime_control.TRANSITION_READY_SERVICE
                        ),
                        True,
                    )

        with patch.object(
            runtime_control.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("docker", 6.0),
        ):
            self.assertIsNone(
                runtime_control._call_operational_trigger(
                    "runtime-container", runtime_control.TRANSITION_READY_SERVICE
                )
            )

    def test_replay_probe_retains_typed_topic_sample(self) -> None:
        topic_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="state: ready\nrunning: false\npaused: false\n",
            stderr="",
        )
        with patch.object(
            runtime_control,
            "_running_mode_container_id",
            return_value="shadow-container",
        ), patch.object(
            runtime_control.subprocess,
            "run",
            return_value=topic_result,
        ) as run:
            self.assertTrue(
                runtime_control.probe_mode_inactive(Path("/workspace"), "replay")
            )
        self.assertIn("ros2 topic echo", run.call_args.args[0][5])

    def test_replay_state_rejects_running_paused_and_unknown(self) -> None:
        self.assertTrue(
            runtime_control.mode_state_is_inactive(
                "replay",
                {"state": "stopped", "running": False, "paused": False},
            )
        )
        for payload in (
            {"state": "running", "running": True, "paused": False},
            {"state": "paused", "running": False, "paused": True},
            {"state": "starting", "running": False, "paused": False},
            {"state": "stopped", "running": False},
        ):
            self.assertFalse(runtime_control.mode_state_is_inactive("replay", payload))

    def test_debug_state_requires_disarmed_monitor_only(self) -> None:
        def status(state: str, armed: bool) -> dict[str, str]:
            return {"data": json.dumps({"session": {"state": state, "armed": armed}})}

        self.assertTrue(
            runtime_control.mode_state_is_inactive(
                "debug", status("MONITOR_ONLY", False)
            )
        )
        self.assertFalse(
            runtime_control.mode_state_is_inactive("debug", status("ARMED", True))
        )
        self.assertFalse(
            runtime_control.mode_state_is_inactive("debug", status("BUSY", False))
        )


class RosbridgeRouteProbeTests(unittest.TestCase):
    def test_nondefault_router_port_and_path_are_used(self) -> None:
        class FakeConnection:
            sent = b""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout: float) -> None:
                pass

            def sendall(self, payload: bytes) -> None:
                self.sent = payload

            def recv(self, _size: int) -> bytes:
                return b"HTTP/1.1 101 Switching Protocols\r\n"

        connection = FakeConnection()
        with patch.object(
            runtime_control.socket,
            "create_connection",
            return_value=connection,
        ) as create_connection:
            ready = runtime_control.websocket_route_ready(
                "replay",
                port=19091,
                route_paths={"replay": "/custom-shadow"},
            )
        self.assertTrue(ready)
        create_connection.assert_called_once_with(("127.0.0.1", 19091), timeout=0.5)
        self.assertIn(b"GET /custom-shadow HTTP/1.1", connection.sent)
        self.assertIn(b"Host: 127.0.0.1:19091", connection.sent)


class RuntimeControlResponseTests(unittest.TestCase):
    @staticmethod
    def handler_with_writer(writer):
        handler = object.__new__(runtime_control.RuntimeControlRequestHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = writer
        return handler

    def test_client_disconnect_during_response_body_is_quiet(self) -> None:
        writer = Mock()
        writer.write.side_effect = BrokenPipeError("client closed")
        handler = self.handler_with_writer(writer)

        handler._send_json(runtime_control.HTTPStatus.ACCEPTED, {"phase": "starting"})

        writer.write.assert_called_once()

    def test_non_connection_response_errors_are_not_hidden(self) -> None:
        writer = Mock()
        writer.write.side_effect = RuntimeError("programming error")
        handler = self.handler_with_writer(writer)

        with self.assertRaisesRegex(RuntimeError, "programming error"):
            handler._send_json(runtime_control.HTTPStatus.OK, {"phase": "idle"})


if __name__ == "__main__":
    unittest.main()
