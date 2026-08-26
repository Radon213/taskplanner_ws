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
            required_plane_probe=lambda _mode: True,
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

    def test_live_transition_defaults_to_virtual_endpoint(self) -> None:
        accepted, snapshot = self.controller.start_transition("live")
        self.assertTrue(accepted)
        self.assertEqual(snapshot.phase, "starting")
        self.assertEqual(
            self.environments[0]["TASKPLANNER_LIVE_ROBOT_ENDPOINT_SOURCE"],
            "virtual",
        )
        self.process.done.set()

    def test_same_healthy_mode_is_an_idle_noop(self) -> None:
        self.write_active_mode("live")
        self.controller._running_modes_probe = lambda: {"live"}
        self.controller._mode_contract_probe = lambda _mode: True
        self.controller._transition_interlock_probe = Mock(
            side_effect=AssertionError("a same-mode no-op must not require stopped state")
        )

        accepted, snapshot = self.controller.start_transition("live")

        self.assertTrue(accepted)
        self.assertEqual(snapshot.phase, "idle")
        self.assertEqual(snapshot.active_mode, "live")
        self.assertIsNone(snapshot.requested_mode)
        self.assertIn("already ready", snapshot.message)
        self.assertEqual(self.commands, [])
        self.controller._transition_interlock_probe.assert_not_called()

    def test_same_mode_is_not_a_noop_when_running_mode_is_ambiguous(self) -> None:
        self.write_active_mode("live")
        self.controller._running_modes_probe = lambda: {"live", "replay"}
        self.controller._transition_interlock_probe = lambda _mode: False

        accepted, snapshot = self.controller.start_transition("live")

        self.assertFalse(accepted)
        self.assertEqual(snapshot.phase, "idle")
        self.assertEqual(snapshot.active_mode, "live")
        self.assertEqual(self.commands, [])

    def test_http_same_healthy_mode_returns_accepted_idle_noop(self) -> None:
        self.write_active_mode("replay")
        self.controller._running_modes_probe = lambda: {"replay"}
        self.controller._mode_contract_probe = lambda _mode: True

        with self.request(
            "POST", "/v1/runtime/transition", {"mode": "replay"}
        ) as response:
            payload = json.loads(response.read())

        self.assertEqual(response.status, 202)
        self.assertEqual(payload["phase"], "idle")
        self.assertEqual(payload["active_mode"], "replay")
        self.assertIsNone(payload["requested_mode"])
        self.assertEqual(self.commands, [])

    def test_same_mode_degraded_required_plane_is_not_a_noop(self) -> None:
        self.write_active_mode("live")
        self.controller._running_modes_probe = lambda: {"live"}
        self.controller._mode_contract_probe = lambda _mode: True
        self.controller._required_plane_probe = lambda _mode: False
        self.controller._transition_interlock_probe = lambda _mode: False

        accepted, snapshot = self.controller.start_transition("live")

        self.assertFalse(accepted)
        self.assertEqual(snapshot.phase, "idle")
        self.assertIn("Stop the active runtime", snapshot.message)
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

    def test_ready_direct_launch_recovers_matching_failed_request(self) -> None:
        self.controller._phase = "failed"
        self.controller._requested_mode = "live"
        self.write_active_mode("live")

        snapshot = self.controller.snapshot()

        self.assertEqual(snapshot.phase, "idle")
        self.assertEqual(snapshot.active_mode, "live")
        self.assertIsNone(snapshot.requested_mode)
        self.assertFalse(snapshot.retryable)

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

    def test_status_invalidates_marker_immediately_when_runtime_profile_mismatches(self) -> None:
        root = Path(self.tempdir.name) / "profile-mismatch"
        root.mkdir()
        state_file = root / "active-runtime-mode.json"
        state_file.write_text('{"mode":"live"}', encoding="utf-8")
        controller = runtime_control.RuntimeController(
            root=root,
            state_file=state_file,
            launcher=root / "scripts" / "taskplanner",
            mode_running_probe=lambda _mode: True,
            mode_contract_probe=lambda mode: False if mode == "live" else True,
        )

        snapshot = controller.snapshot()

        self.assertEqual(snapshot.phase, "failed")
        self.assertIsNone(snapshot.active_mode)
        self.assertEqual(snapshot.requested_mode, "live")
        self.assertEqual(
            snapshot.diagnostic_code,
            runtime_control.RUNTIME_PROFILE_MISMATCH_CODE,
        )
        self.assertTrue(snapshot.retryable)
        self.assertFalse(state_file.exists())

    def test_legacy_mock_container_is_classified_for_a_safe_replacement_but_not_accepted_as_marked(self) -> None:
        environment = {
            "INPUT_PROFILE": "simulation",
            "EXECUTION_BACKEND": "mock",
        }

        self.assertEqual(
            runtime_control.classify_taskplanner_runtime_environment(environment),
            "llm-surgeon",
        )
        self.assertIsNone(
            runtime_control.taskplanner_runtime_mode_from_environment(environment),
        )

    def test_status_surfaces_an_unmarked_running_core_after_controller_restart(self) -> None:
        root = Path(self.tempdir.name) / "unmarked-core"
        root.mkdir()
        controller = runtime_control.RuntimeController(
            root=root,
            state_file=root / "active-runtime-mode.json",
            launcher=root / "scripts" / "taskplanner",
            running_modes_probe=lambda: {"llm-surgeon"},
        )

        snapshot = controller.snapshot()

        self.assertEqual(snapshot.phase, "failed")
        self.assertIsNone(snapshot.active_mode)
        self.assertEqual(snapshot.requested_mode, "llm-surgeon")
        self.assertEqual(
            snapshot.diagnostic_code,
            runtime_control.RUNTIME_PROFILE_MISMATCH_CODE,
        )
        self.assertTrue(snapshot.retryable)

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
        self.assertEqual(snapshot.phase, "failed")
        self.assertEqual(
            snapshot.diagnostic_code,
            runtime_control.RUNTIME_PROFILE_MISMATCH_CODE,
        )
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
                running_modes_probe=lambda: {"llm-surgeon"},
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
        self.assertIn(
            "/workspaces/taskplanner_ws/install/docker/setup.bash",
            command[5],
        )
        self.assertNotIn(
            "/workspaces/taskplanner_ws/install/setup.bash",
            command[5],
        )
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
        self.assertIn(
            "/workspaces/taskplanner_ws/install/docker/setup.bash",
            run.call_args.args[0][5],
        )
        self.assertNotIn(
            "/workspaces/taskplanner_ws/install/setup.bash",
            run.call_args.args[0][5],
        )

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


class RequiredPlaneProbeTests(unittest.TestCase):
    @staticmethod
    def docker_result(*services: str, unhealthy: str | None = None):
        lines = []
        for service in services:
            health = "(unhealthy)" if service == unhealthy else "(healthy)"
            lines.append(f"{service}\trunning\tUp 10 seconds {health}")
        return subprocess.CompletedProcess([], 0, "\n".join(lines) + "\n", "")

    def test_live_requires_every_healthy_mandatory_sidecar(self) -> None:
        required = (
            "ninfer-manager",
            "webapp",
            "public-rosbridge",
            "taskplanner-asr",
        )
        with patch.object(
            runtime_control.subprocess,
            "run",
            return_value=self.docker_result(*required),
        ):
            self.assertTrue(
                runtime_control.mode_required_plane_ready(
                    Path("/workspace"), "live"
                )
            )

        with patch.object(
            runtime_control.subprocess,
            "run",
            return_value=self.docker_result(*required[:-1]),
        ):
            self.assertFalse(
                runtime_control.mode_required_plane_ready(
                    Path("/workspace"), "live"
                )
            )

        with patch.object(
            runtime_control.subprocess,
            "run",
            return_value=self.docker_result(*required, unhealthy="taskplanner-asr"),
        ):
            self.assertFalse(
                runtime_control.mode_required_plane_ready(
                    Path("/workspace"), "live"
                )
            )

    def test_non_live_operational_plane_does_not_require_asr(self) -> None:
        result = self.docker_result(
            "ninfer-manager", "webapp", "public-rosbridge"
        )
        with patch.object(runtime_control.subprocess, "run", return_value=result):
            self.assertTrue(
                runtime_control.mode_required_plane_ready(
                    Path("/workspace"), "replay"
                )
            )

    def test_required_plane_probe_transport_failure_is_unknown(self) -> None:
        with patch.object(
            runtime_control.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("docker", 1.0),
        ):
            self.assertIsNone(
                runtime_control.mode_required_plane_ready(
                    Path("/workspace"), "live"
                )
            )


class RosbridgeRouteProbeTests(unittest.TestCase):
    def test_live_core_probe_uses_declared_loopback_bridge_not_optional_router(self) -> None:
        root = Path("/workspace")
        with patch.object(
            runtime_control,
            "_running_mode_container_id",
            return_value="runtime-container",
        ), patch.object(
            runtime_control,
            "_container_environment",
            return_value={"ROSBRIDGE_PORT": "19090"},
        ), patch.object(
            runtime_control,
            "websocket_route_ready",
            return_value=True,
        ) as readiness:
            self.assertTrue(runtime_control.core_runtime_rosbridge_ready(root, "live"))

        readiness.assert_called_once_with(
            "live", port=19090, route_paths={"live": "/"}
        )

    def test_live_compose_probe_does_not_require_optional_router(self) -> None:
        result = subprocess.CompletedProcess([], 0, "runtime-container\n", "")
        with patch.object(
            runtime_control.subprocess,
            "run",
            return_value=result,
        ), patch.object(
            runtime_control,
            "mode_runtime_contract_matches",
            return_value=True,
        ), patch.object(
            runtime_control,
            "core_runtime_rosbridge_ready",
            return_value=True,
        ) as core_readiness, patch.object(
            runtime_control,
            "websocket_route_ready",
        ) as router_readiness:
            self.assertTrue(
                runtime_control.compose_service_running(
                    Path("/workspace"), "live", router_port=19091
                )
            )

        core_readiness.assert_called_once_with(Path("/workspace"), "live")
        router_readiness.assert_not_called()

    def test_live_core_probe_rejects_missing_or_invalid_declared_port(self) -> None:
        root = Path("/workspace")
        for environment in (
            {},
            {"ROSBRIDGE_PORT": "not-a-port"},
            {"ROSBRIDGE_PORT": "0"},
        ):
            with self.subTest(environment=environment), patch.object(
                runtime_control,
                "_running_mode_container_id",
                return_value="runtime-container",
            ), patch.object(
                runtime_control,
                "_container_environment",
                return_value=environment,
            ):
                self.assertFalse(runtime_control.core_runtime_rosbridge_ready(root, "live"))

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


class FakeAsrDocker:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.container_id = "a" * 12
        self.image_id = "sha256:" + "c" * 64
        self.user = "1000:1000"
        self.commands: list[list[str]] = []
        self.restarted = False
        self.initial_status = "running"
        self.initial_running = True
        self.initial_restarting = False
        self.initial_pid = 101
        self.initial_health = "healthy"
        self.restart_seen = threading.Event()
        self.restart_gate: threading.Event | None = None
        self.import_returncode = 0
        self.after_health = "healthy"
        self.graph_timeout = False
        self.topic_graph_outputs = [
            """Type: std_msgs/msg/String
Publisher count: 1
Node name: taskplanner_asr
Node namespace: /
Endpoint type: PUBLISHER
GID: 01.10.2d.67.14.cc.59.b2.e9.63.7d.02.00.00.16.03
Subscription count: 0
"""
        ]
        self.service_graph_outputs = [
            """Type: surgical_msgs/srv/AsrControl
Clients count: 0
Services count: 1
"""
        ]
        self.node_graph_outputs = [
            """/taskplanner_asr
  Subscribers:

  Publishers:
    /input/asr/runtime_status: std_msgs/msg/String
  Service Servers:
    /input/asr/control: surgical_msgs/srv/AsrControl
    /taskplanner_asr/get_parameters: rcl_interfaces/srv/GetParameters
  Service Clients:

  Action Servers:

  Action Clients:
"""
        ]

    def __call__(self, command: list[str], **_kwargs: object):
        self.commands.append(command)
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, f"{self.container_id}\n", "")
        if command[:3] == ["docker", "inspect", "--format"]:
            if command[3] == "{{json .Config.Labels}}":
                labels = {
                    "com.docker.compose.project.working_dir": str(self.root),
                    "com.docker.compose.service": runtime_control.ASR_COMPOSE_SERVICE,
                }
                return subprocess.CompletedProcess(command, 0, json.dumps(labels), "")
            if command[3] == "{{json .Image}}":
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(self.image_id), ""
                )
            if command[3] == "{{json .Config.User}}":
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(self.user), ""
                )
            if command[3] == "{{json .Config.WorkingDir}}":
                return subprocess.CompletedProcess(
                    command, 0, json.dumps("/workspaces/taskplanner_ws"), ""
                )
            if command[3] == "{{json .Mounts}}":
                mounts = [
                    {
                        "Type": "bind",
                        "Source": str(self.root),
                        "Destination": "/workspaces/taskplanner_ws",
                        "RW": True,
                    }
                ]
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(mounts), ""
                )
            state = {
                "Status": "running" if self.restarted else self.initial_status,
                "Running": True if self.restarted else self.initial_running,
                "Restarting": False if self.restarted else self.initial_restarting,
                "Pid": 202 if self.restarted else self.initial_pid,
                "StartedAt": (
                    "2026-08-27T01:02:04.000000000Z"
                    if self.restarted
                    else "2026-08-27T01:02:03.000000000Z"
                ),
                "Health": {
                    "Status": (
                        self.after_health if self.restarted else self.initial_health
                    )
                },
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(state), "")
        if command[:2] == ["docker", "exec"]:
            shell_command = command[5]
            if shell_command == runtime_control.ASR_TOPIC_GRAPH_PROBE_SHELL:
                if self.graph_timeout:
                    raise subprocess.TimeoutExpired(
                        command, float(_kwargs.get("timeout", 0.0))
                    )
                output = self.topic_graph_outputs.pop(0)
                if not self.topic_graph_outputs:
                    self.topic_graph_outputs.append(output)
                return subprocess.CompletedProcess(command, 0, output, "")
            if shell_command == runtime_control.ASR_SERVICE_GRAPH_PROBE_SHELL:
                output = self.service_graph_outputs.pop(0)
                if not self.service_graph_outputs:
                    self.service_graph_outputs.append(output)
                return subprocess.CompletedProcess(command, 0, output, "")
            if shell_command == runtime_control.ASR_NODE_GRAPH_PROBE_SHELL:
                output = self.node_graph_outputs.pop(0)
                if not self.node_graph_outputs:
                    self.node_graph_outputs.append(output)
                return subprocess.CompletedProcess(command, 0, output, "")
            if shell_command != runtime_control.ASR_IMPORT_PROBE_SHELL:
                raise AssertionError(f"unexpected docker exec: {command!r}")
            return subprocess.CompletedProcess(
                command,
                self.import_returncode,
                f"{runtime_control.ASR_EXPECTED_IMPORT_PATH}\n",
                "import failed" if self.import_returncode else "",
            )
        if command[:2] == ["docker", "restart"]:
            self.restart_seen.set()
            if self.restart_gate is not None:
                self.restart_gate.wait(timeout=2)
            self.restarted = True
            return subprocess.CompletedProcess(command, 0, f"{self.container_id}\n", "")
        if command[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(
                command, 0, f"{runtime_control.ASR_EXPECTED_IMPORT_PATH}\n", ""
            )
        raise AssertionError(f"unexpected command: {command!r}")


class AsrRestartApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_file = self.root / "active-runtime-mode.json"
        self.state_file.write_text('{"mode":"live"}', encoding="utf-8")
        self.docker = FakeAsrDocker(self.root)
        self.source_revision = "b" * 64
        self.controller = runtime_control.RuntimeController(
            root=self.root,
            state_file=self.state_file,
            launcher=self.root / "scripts" / "taskplanner",
            mode_running_probe=lambda _mode: True,
            mode_contract_probe=lambda _mode: True,
            required_plane_probe=lambda _mode: True,
            running_modes_probe=lambda: {"live"},
            command_runner=self.docker,
            asr_install_contract_probe=lambda: True,
            asr_source_revision_probe=lambda: self.source_revision,
            asr_restart_health_timeout_sec=0.2,
            asr_restart_poll_interval_sec=0.001,
            asr_graph_verify_timeout_sec=0.05,
            asr_graph_verify_poll_interval_sec=0.001,
        )
        self.token = "a" * 48
        self.server = runtime_control.create_server(
            "127.0.0.1", 0, self.controller, self.token
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        if self.docker.restart_gate is not None:
            self.docker.restart_gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.tempdir.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        token: bool = True,
        request_id: str | None = "11111111-1111-4111-8111-111111111111",
    ):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if token:
            headers[runtime_control.TOKEN_HEADER] = self.token
        if path == "/v1/runtime/asr/restart" and request_id is not None:
            headers[runtime_control.REQUEST_ID_HEADER] = request_id
        return urlopen(
            Request(
                f"{self.base_url}{path}",
                data=body,
                headers=headers,
                method=method,
            ),
            timeout=2,
        )

    def wait_for_terminal(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshot = self.controller.asr_restart_snapshot()
            if snapshot.phase in {"succeeded", "failed"}:
                return snapshot
            time.sleep(0.005)
        self.fail("ASR restart did not reach a terminal state")

    def test_restart_is_async_allowlisted_and_reports_verified_new_process(self) -> None:
        gate = threading.Event()
        self.docker.restart_gate = gate

        with self.request("GET", "/v1/runtime/asr/status") as response:
            baseline = json.loads(response.read())
        self.assertEqual(baseline["generation"], 0)
        self.assertIsNone(baseline["request_id"])

        with self.request("POST", "/v1/runtime/asr/restart", {}) as response:
            self.assertEqual(response.status, 202)
            accepted = json.loads(response.read())
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["phase"], "queued")
        self.assertEqual(accepted["generation"], 1)
        self.assertRegex(accepted["job_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(
            accepted["request_id"], "11111111-1111-4111-8111-111111111111"
        )
        self.assertTrue(self.docker.restart_seen.wait(timeout=1))

        with self.request("GET", "/v1/runtime/asr/status") as response:
            in_progress = json.loads(response.read())
        self.assertEqual(in_progress["job_id"], accepted["job_id"])
        self.assertEqual(in_progress["phase"], "restarting")
        self.assertEqual(in_progress["request_id"], accepted["request_id"])

        gate.set()
        snapshot = self.wait_for_terminal()
        self.assertEqual(snapshot.phase, "succeeded")
        self.assertEqual(snapshot.source_revision, self.source_revision)
        self.assertEqual(snapshot.request_id, accepted["request_id"])
        self.assertEqual(snapshot.before_pid, 101)
        self.assertEqual(snapshot.after_pid, 202)
        self.assertEqual(
            snapshot.container_started_at, "2026-08-27T01:02:04.000000000Z"
        )
        self.assertTrue((self.state_file.parent / "launcher.lock").exists())

        restart_commands = [
            command
            for command in self.docker.commands
            if command[:2] == ["docker", "restart"]
        ]
        self.assertEqual(
            restart_commands,
            [["docker", "restart", "--time", "45", self.docker.container_id]],
        )
        resolve_command = next(
            command
            for command in self.docker.commands
            if command[:2] == ["docker", "ps"]
        )
        self.assertIn(
            f"label=com.docker.compose.project.working_dir={self.root.resolve()}",
            resolve_command,
        )
        self.assertIn(
            "label=com.docker.compose.service=taskplanner-asr", resolve_command
        )

    def test_restart_and_mode_transition_are_mutually_serialized(self) -> None:
        gate = threading.Event()
        self.docker.restart_gate = gate
        with self.request("POST", "/v1/runtime/asr/restart", {}) as response:
            first = json.loads(response.read())
        self.assertTrue(self.docker.restart_seen.wait(timeout=1))

        with self.assertRaises(HTTPError) as duplicate_error:
            self.request(
                "POST",
                "/v1/runtime/asr/restart",
                {},
                request_id="22222222-2222-4222-8222-222222222222",
            )
        self.assertEqual(duplicate_error.exception.code, 409)
        duplicate = json.loads(duplicate_error.exception.read())
        duplicate_error.exception.close()
        self.assertFalse(duplicate["accepted"])
        self.assertEqual(duplicate["job_id"], first["job_id"])
        self.assertEqual(duplicate["request_id"], first["request_id"])
        self.assertNotEqual(
            duplicate["request_id"], "22222222-2222-4222-8222-222222222222"
        )

        with self.assertRaises(HTTPError) as transition_error:
            self.request("POST", "/v1/runtime/transition", {"mode": "replay"})
        self.assertEqual(transition_error.exception.code, 409)
        transition = json.loads(transition_error.exception.read())
        transition_error.exception.close()
        self.assertIn("ASR node restart", transition["message"])
        gate.set()
        self.assertEqual(self.wait_for_terminal().phase, "succeeded")

    def test_stopped_container_uses_same_image_isolated_preflight_then_recovers(self) -> None:
        self.docker.initial_status = "exited"
        self.docker.initial_running = False
        self.docker.initial_pid = 0
        self.docker.initial_health = "unavailable"

        accepted, _snapshot = self.controller.start_asr_restart()
        self.assertTrue(accepted)
        snapshot = self.wait_for_terminal()

        self.assertEqual(snapshot.phase, "succeeded")
        self.assertEqual(snapshot.before_pid, 0)
        self.assertEqual(snapshot.after_pid, 202)
        isolated_commands = [
            command
            for command in self.docker.commands
            if command[:2] == ["docker", "run"]
        ]
        self.assertEqual(len(isolated_commands), 1)
        isolated = isolated_commands[0]
        self.assertIn("--rm", isolated)
        self.assertIn("--read-only", isolated)
        self.assertIn("--network", isolated)
        self.assertEqual(isolated[isolated.index("--network") + 1], "none")
        self.assertEqual(isolated[isolated.index("--user") + 1], self.docker.user)
        self.assertIn(self.docker.image_id, isolated)
        self.assertEqual(isolated.count("--volume"), 1)
        self.assertIn(
            f"{self.root.resolve()}:/workspaces/taskplanner_ws:rw", isolated
        )
        self.assertFalse(any("/taskplanner-runs" in value for value in isolated))
        self.assertFalse(any("Downloads" in value for value in isolated))
        preflight_execs = [
            command
            for command in self.docker.commands
            if command[:2] == ["docker", "exec"]
            and command[5] == runtime_control.ASR_IMPORT_PROBE_SHELL
        ]
        self.assertEqual(preflight_execs, [])
        self.assertEqual(
            [
                command
                for command in self.docker.commands
                if command[:2] == ["docker", "restart"]
            ],
            [["docker", "restart", "--time", "45", self.docker.container_id]],
        )

    def test_restarting_crash_loop_uses_isolated_preflight(self) -> None:
        self.docker.initial_status = "restarting"
        self.docker.initial_running = True
        self.docker.initial_restarting = True
        self.docker.initial_pid = 0
        self.docker.initial_health = "starting"

        accepted, _snapshot = self.controller.start_asr_restart()
        self.assertTrue(accepted)
        snapshot = self.wait_for_terminal()

        self.assertEqual(snapshot.phase, "succeeded")
        self.assertTrue(
            any(
                command[:2] == ["docker", "run"]
                for command in self.docker.commands
            )
        )

    def test_stopped_recovery_rejects_nonimmutable_image_before_docker_run(self) -> None:
        self.docker.initial_status = "exited"
        self.docker.initial_running = False
        self.docker.initial_pid = 0
        self.docker.image_id = "taskplanner-ws:dev"

        accepted, _snapshot = self.controller.start_asr_restart()
        self.assertTrue(accepted)
        snapshot = self.wait_for_terminal()

        self.assertEqual(snapshot.phase, "failed")
        self.assertIn("identity", snapshot.message)
        self.assertFalse(
            any(
                command[:2] in (["docker", "run"], ["docker", "restart"])
                for command in self.docker.commands
            )
        )

    def test_restart_requires_token_exact_empty_object_and_live_mode(self) -> None:
        with self.assertRaises(HTTPError) as unauthorized:
            self.request("POST", "/v1/runtime/asr/restart", {}, token=False)
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()

        for request_id in (
            None,
            "",
            "not-a-uuid",
            "11111111-1111-1111-8111-111111111111",
            "11111111-1111-4111-8111-11111111111A",
            "11111111-1111-4111-8111-111111111111 ",
        ):
            with self.subTest(request_id=request_id), self.assertRaises(
                HTTPError
            ) as invalid_request_id:
                self.request(
                    "POST",
                    "/v1/runtime/asr/restart",
                    {},
                    request_id=request_id,
                )
            self.assertEqual(invalid_request_id.exception.code, 400)
            invalid_request_id.exception.close()

        for invalid_payload in ({"service": "other"}, [], "{}"):
            with self.subTest(payload=invalid_payload), self.assertRaises(
                HTTPError
            ) as invalid:
                self.request("POST", "/v1/runtime/asr/restart", invalid_payload)
            self.assertEqual(invalid.exception.code, 400)
            invalid.exception.close()

        self.state_file.write_text('{"mode":"debug"}', encoding="utf-8")
        self.controller._running_modes_probe = lambda: {"debug"}
        rejected_request_id = "33333333-3333-4333-8333-333333333333"
        with self.assertRaises(HTTPError) as wrong_mode:
            self.request(
                "POST",
                "/v1/runtime/asr/restart",
                {},
                request_id=rejected_request_id,
            )
        self.assertEqual(wrong_mode.exception.code, 409)
        payload = json.loads(wrong_mode.exception.read())
        wrong_mode.exception.close()
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["phase"], "failed")
        self.assertEqual(payload["request_id"], rejected_request_id)
        with self.request("GET", "/v1/runtime/asr/status") as response:
            persisted = json.loads(response.read())
        self.assertIsNone(persisted["request_id"])
        self.assertEqual(self.docker.commands, [])

    def test_abi_mismatch_refuses_restart_before_any_docker_command(self) -> None:
        self.controller._asr_install_contract_probe = lambda: False
        accepted, queued = self.controller.start_asr_restart()
        self.assertTrue(accepted)
        self.assertEqual(queued.phase, "queued")

        snapshot = self.wait_for_terminal()
        self.assertEqual(snapshot.phase, "failed")
        self.assertFalse(snapshot.retryable)
        self.assertIn("install contract", snapshot.message)
        self.assertEqual(self.docker.commands, [])

    def test_import_failure_never_interrupts_the_running_asr_container(self) -> None:
        self.docker.import_returncode = 1
        accepted, _snapshot = self.controller.start_asr_restart()
        self.assertTrue(accepted)

        snapshot = self.wait_for_terminal()
        self.assertEqual(snapshot.phase, "failed")
        self.assertTrue(snapshot.retryable)
        self.assertFalse(self.docker.restarted)
        self.assertFalse(
            any(
                command[:2] == ["docker", "restart"]
                for command in self.docker.commands
            )
        )

    def test_preflight_purges_then_checked_hash_compiles_before_fresh_import(self) -> None:
        runtime_control.preflight_asr_import(
            self.docker.container_id, self.docker
        )

        command = self.docker.commands[-1]
        self.assertEqual(
            command[:-1],
            [
                "docker",
                "exec",
                self.docker.container_id,
                "bash",
                "-lc",
                runtime_control.ASR_IMPORT_PROBE_SHELL,
                "--",
            ],
        )
        probe_code = command[-1]
        self.assertIn("importlib.util.cache_from_source", probe_code)
        self.assertIn("py_compile.PycInvalidationMode.CHECKED_HASH", probe_code)
        self.assertLess(
            probe_code.index("cache_path.unlink"),
            probe_code.index("py_compile.compile"),
        )
        self.assertLess(
            probe_code.index("py_compile.compile"),
            probe_code.index(
                'importlib.import_module("integration_debug.operational_asr_node")'
            ),
        )
        for filename in runtime_control.ASR_SOURCE_MANIFEST:
            self.assertIn(f'"{filename}"', probe_code)
        self.assertNotIn("glob(", probe_code)
        self.assertNotIn("rglob(", probe_code)

    def test_ros_graph_probe_requires_unique_typed_topic_and_service(self) -> None:
        runtime_control.verify_unique_asr_status_publisher(
            self.docker.container_id, self.docker
        )

        self.docker.topic_graph_outputs = [
            self.docker.topic_graph_outputs[0].replace(
                "Publisher count: 1", "Publisher count: 2"
            )
        ]
        with self.assertRaises(runtime_control.AsrRestartError):
            runtime_control.verify_unique_asr_status_publisher(
                self.docker.container_id, self.docker
            )

        clean_docker = FakeAsrDocker(self.root)
        clean_docker.node_graph_outputs = [
            clean_docker.node_graph_outputs[0].replace(
                "    /input/asr/control: surgical_msgs/srv/AsrControl\n", ""
            )
        ]
        with self.assertRaises(runtime_control.AsrRestartError):
            runtime_control.verify_unique_asr_status_publisher(
                clean_docker.container_id, clean_docker
            )

        self.docker.topic_graph_outputs = [FakeAsrDocker(self.root).topic_graph_outputs[0]]
        self.docker.service_graph_outputs = [
            self.docker.service_graph_outputs[0].replace(
                "Services count: 1", "Services count: 2"
            )
        ]
        with self.assertRaises(runtime_control.AsrRestartError):
            runtime_control.verify_unique_asr_status_publisher(
                self.docker.container_id, self.docker
            )

    def test_restart_retries_until_old_dds_publisher_disappears(self) -> None:
        valid_topic = self.docker.topic_graph_outputs[0]
        self.docker.topic_graph_outputs = [
            valid_topic.replace("Publisher count: 1", "Publisher count: 2"),
            valid_topic,
        ]
        accepted, _snapshot = self.controller.start_asr_restart()
        self.assertTrue(accepted)

        snapshot = self.wait_for_terminal()
        self.assertEqual(snapshot.phase, "succeeded")
        topic_probes = [
            command
            for command in self.docker.commands
            if command[:2] == ["docker", "exec"]
            and command[5] == runtime_control.ASR_TOPIC_GRAPH_PROBE_SHELL
        ]
        self.assertEqual(len(topic_probes), 2)

    def test_persistent_duplicate_ros_endpoint_is_not_success(self) -> None:
        self.controller._asr_graph_verify_timeout_sec = 0.01
        self.controller._asr_graph_verify_poll_interval_sec = 0.005
        self.docker.topic_graph_outputs = [
            self.docker.topic_graph_outputs[0].replace(
                "Publisher count: 1", "Publisher count: 2"
            )
        ]
        accepted, _snapshot = self.controller.start_asr_restart()
        self.assertTrue(accepted)

        snapshot = self.wait_for_terminal()
        self.assertEqual(snapshot.phase, "failed")
        self.assertIn("unique on the ROS graph", snapshot.message)
        self.assertIsNone(snapshot.after_pid)

    def test_graph_subprocess_timeout_respects_absolute_retry_deadline(self) -> None:
        self.controller._asr_graph_verify_timeout_sec = 0.02
        self.controller._asr_graph_verify_poll_interval_sec = 0.005
        self.docker.graph_timeout = True
        started = time.monotonic()
        accepted, _snapshot = self.controller.start_asr_restart()
        self.assertTrue(accepted)

        snapshot = self.wait_for_terminal()
        elapsed = time.monotonic() - started
        self.assertEqual(snapshot.phase, "failed")
        self.assertLess(elapsed, 0.25)

    def test_unhealthy_new_pid_is_not_reported_as_success(self) -> None:
        self.docker.after_health = "unhealthy"
        accepted, _snapshot = self.controller.start_asr_restart()
        self.assertTrue(accepted)

        snapshot = self.wait_for_terminal()
        self.assertEqual(snapshot.phase, "failed")
        self.assertEqual(snapshot.before_pid, 101)
        self.assertIsNone(snapshot.after_pid)
        self.assertIn("did not become healthy", snapshot.message)

    def test_source_change_during_preflight_fails_before_restart(self) -> None:
        revisions = iter(["1" * 64, "2" * 64])
        self.controller._asr_source_revision_probe = lambda: next(revisions)
        accepted, _snapshot = self.controller.start_asr_restart()
        self.assertTrue(accepted)

        snapshot = self.wait_for_terminal()
        self.assertEqual(snapshot.phase, "failed")
        self.assertIn("changed during preflight", snapshot.message)
        self.assertFalse(self.docker.restarted)

    def test_source_revision_matches_node_manifest_algorithm(self) -> None:
        package_dir = (
            self.root / "src" / "integration_debug" / "integration_debug"
        )
        package_dir.mkdir(parents=True)
        expected_digest = runtime_control.hashlib.sha256()
        for index, filename in enumerate(runtime_control.ASR_SOURCE_MANIFEST):
            content = f"source-{index}".encode("utf-8")
            (package_dir / filename).write_bytes(content)
            encoded_name = filename.encode("utf-8")
            expected_digest.update(len(encoded_name).to_bytes(2, "big"))
            expected_digest.update(encoded_name)
            expected_digest.update(b"\x00present")
            expected_digest.update(len(content).to_bytes(8, "big"))
            expected_digest.update(content)

        self.assertEqual(
            runtime_control.asr_source_revision(self.root),
            expected_digest.hexdigest(),
        )

    def _create_asr_install_contract_tree(self, name: str) -> Path:
        root = self.root / name
        for index, relative in enumerate(
            runtime_control.ASR_ABI_CONTRACT_RELATIVE_PATHS
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"contract-{index}\n", encoding="utf-8")
        unrelated = root / "src" / "unrelated" / "msg" / "Other.msg"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("string value\n", encoding="utf-8")
        shared_cmake = root / "src" / "surgical_msgs" / "CMakeLists.txt"
        shared_cmake.parent.mkdir(parents=True, exist_ok=True)
        shared_cmake.write_text("# unrelated interface list\n", encoding="utf-8")
        install_root = root / "install" / "docker"
        entrypoint = (
            install_root
            / "integration_debug"
            / "lib"
            / "integration_debug"
            / "operational_asr_node"
        )
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
        entrypoint.chmod(0o755)
        (install_root / "setup.bash").write_text("# setup\n", encoding="utf-8")
        return root

    def test_asr_contract_bootstrap_then_ignores_unrelated_abi_edits(self) -> None:
        root = self._create_asr_install_contract_tree("scoped-contract")
        global_marker = (
            root / "install" / "docker" / ".taskplanner-runtime-abi-contract-v1"
        )
        global_marker.write_text(
            runtime_control.runtime_install_contract_fingerprint(root) + "\n",
            encoding="utf-8",
        )

        self.assertTrue(runtime_control.ensure_asr_install_contract(root))
        scoped_marker = (
            root
            / "install"
            / "docker"
            / runtime_control.ASR_ABI_CONTRACT_MARKER
        )
        self.assertTrue(scoped_marker.is_file())
        self.assertTrue(runtime_control.asr_install_contract_matches_source(root))

        unrelated = root / "src" / "unrelated" / "msg" / "Other.msg"
        unrelated.write_text("string changed\n", encoding="utf-8")
        shared_cmake = root / "src" / "surgical_msgs" / "CMakeLists.txt"
        shared_cmake.write_text("# another unrelated interface\n", encoding="utf-8")
        self.assertFalse(runtime_control.runtime_install_contract_matches_source(root))
        self.assertTrue(runtime_control.ensure_asr_install_contract(root))

        relevant = root / "src" / "surgical_msgs" / "srv" / "AsrControl.srv"
        relevant.write_text("string changed\n---\nbool accepted\n", encoding="utf-8")
        self.assertFalse(runtime_control.ensure_asr_install_contract(root))

    def test_each_imported_asr_interface_and_package_dependency_is_scoped(self) -> None:
        relevant_paths = (
            "src/surgical_msgs/srv/AsrControl.srv",
            "src/surgical_msgs/msg/SpeechUtterance.msg",
            "src/surgical_msgs/package.xml",
        )
        for index, relative in enumerate(relevant_paths):
            with self.subTest(relative=relative):
                root = self._create_asr_install_contract_tree(f"relevant-{index}")
                global_marker = (
                    root
                    / "install"
                    / "docker"
                    / ".taskplanner-runtime-abi-contract-v1"
                )
                global_marker.write_text(
                    runtime_control.runtime_install_contract_fingerprint(root)
                    + "\n",
                    encoding="utf-8",
                )
                self.assertTrue(runtime_control.ensure_asr_install_contract(root))
                (root / relative).write_text("changed\n", encoding="utf-8")
                self.assertFalse(runtime_control.ensure_asr_install_contract(root))

    def test_asr_contract_never_bootstraps_from_stale_global_marker(self) -> None:
        root = self._create_asr_install_contract_tree("stale-global")
        global_marker = (
            root / "install" / "docker" / ".taskplanner-runtime-abi-contract-v1"
        )
        global_marker.write_text("0" * 64 + "\n", encoding="utf-8")

        self.assertFalse(runtime_control.ensure_asr_install_contract(root))
        self.assertFalse(
            (
                root
                / "install"
                / "docker"
                / runtime_control.ASR_ABI_CONTRACT_MARKER
            ).exists()
        )

    def test_container_resolution_rejects_zero_or_multiple_label_matches(self) -> None:
        for output in ("", f"{'a' * 12}\n{'b' * 12}\n"):
            with self.subTest(output=output):
                runner = Mock(
                    return_value=subprocess.CompletedProcess([], 0, output, "")
                )
                with self.assertRaises(runtime_control.AsrRestartError):
                    runtime_control.resolve_asr_container(self.root, runner)

                command = runner.call_args.args[0]
                self.assertIn(
                    "label=com.docker.compose.service=taskplanner-asr", command
                )
                self.assertIn("-a", command)
                self.assertNotIn("status=running", command)


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
