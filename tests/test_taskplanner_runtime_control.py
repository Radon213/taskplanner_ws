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

    def request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        token: bool = True,
        extra_headers: dict[str, str] | None = None,
    ):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if token:
            headers[runtime_control.TOKEN_HEADER] = self.token
        if extra_headers:
            headers.update(extra_headers)
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        return urlopen(request, timeout=2)

    def test_status_requires_token(self) -> None:
        with self.assertRaises(HTTPError) as context:
            self.request("GET", "/v1/runtime/status", token=False)
        self.assertEqual(context.exception.code, 401)
        context.exception.close()

    def test_owner_status_uses_one_registry_projection_contract(self) -> None:
        owner_rows = [
            {
                "owner": "command",
                "mode": "live",
                "state": "running",
                "service": "taskplanner-command",
                "detail": "Up 2 seconds",
            }
        ]
        with patch.object(
            self.controller, "owner_status", return_value=owner_rows
        ) as status:
            with self.request("GET", "/v1/runtime/owners?mode=live") as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
        self.assertEqual(payload, {"mode": "live", "owners": owner_rows})
        status.assert_called_once_with("live")

    def test_owner_restart_requires_fixed_schema_and_request_id(self) -> None:
        request_id = "12345678-1234-4123-8123-123456789abc"
        with patch.object(
            self.controller,
            "restart_owner",
            return_value=(True, "The owner restarted."),
        ) as restart:
            with self.request(
                "POST",
                "/v1/runtime/owners/restart",
                {"owner": "command", "mode": "live"},
                extra_headers={runtime_control.REQUEST_ID_HEADER: request_id},
            ) as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
        self.assertEqual(
            payload,
            {
                "accepted": True,
                "owner": "command",
                "mode": "live",
                "message": "The owner restarted.",
            },
        )
        restart.assert_called_once_with("command", "live")

        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/runtime/owners/restart",
                {"owner": "command", "mode": "live", "shell": "id"},
                extra_headers={runtime_control.REQUEST_ID_HEADER: request_id},
            )
        self.assertEqual(context.exception.code, 400)
        context.exception.close()

    def test_surgimate_endpoint_has_fixed_schema_and_debug_action_contract(self) -> None:
        request_id = "12345678-1234-4123-8123-123456789abc"
        row = {
            "owner": "surgimate",
            "mode": "debug",
            "state": "running",
            "service": "taskplanner-surgimate",
            "detail": "Up 2 seconds",
        }
        with patch.object(self.controller, "surgimate_status", return_value=row) as status:
            with self.request("GET", "/v1/runtime/surgimate") as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
        self.assertEqual(payload["status"], row)
        status.assert_called_once_with()

        with patch.object(
            self.controller,
            "control_surgimate",
            return_value=(True, "SurgiMate restarted."),
        ) as control:
            with self.request(
                "POST",
                "/v1/runtime/surgimate",
                {"action": "restart"},
                extra_headers={runtime_control.REQUEST_ID_HEADER: request_id},
            ) as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
        self.assertEqual(
            payload,
            {
                "accepted": True,
                "action": "restart",
                "message": "SurgiMate restarted.",
            },
        )
        control.assert_called_once_with("restart")

        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/runtime/surgimate",
                {"action": "restart", "shell": "id"},
                extra_headers={runtime_control.REQUEST_ID_HEADER: request_id},
            )
        self.assertEqual(context.exception.code, 400)
        context.exception.close()

    def test_lifecycle_uses_one_allowlisted_operation_and_request_id(self) -> None:
        request_id = "12345678-1234-4123-8123-123456789abc"
        snapshot = runtime_control.RuntimeLifecycleSnapshot(
            phase="queued",
            generation=1,
            job_id="job",
            request_id=request_id,
            operation="qwen_load",
            active_mode="live",
            message="Runtime lifecycle request is queued.",
            retryable=False,
            ninfer=runtime_control.NInferSnapshot(
                available=True,
                model_id="qwen3.6-35b-a3b",
                model_state="unloaded",
                detail="ready",
            ),
        )
        with patch.object(
            self.controller,
            "start_lifecycle",
            return_value=(True, snapshot),
        ) as lifecycle:
            with self.request(
                "POST",
                "/v1/runtime/lifecycle",
                {"operation": "qwen_load"},
                extra_headers={runtime_control.REQUEST_ID_HEADER: request_id},
            ) as response:
                self.assertEqual(response.status, 202)
                payload = json.loads(response.read())
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["operation"], "qwen_load")
        lifecycle.assert_called_once_with("qwen_load", request_id)

        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/runtime/lifecycle",
                {"operation": "qwen_load", "shell": "id"},
                extra_headers={runtime_control.REQUEST_ID_HEADER: request_id},
            )
        self.assertEqual(context.exception.code, 400)
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
            [[str(Path(self.tempdir.name) / "scripts" / "taskplanner"), "up", "replay"]],
        )
        self.assertEqual(
            self.environments[0]["TASKPLANNER_RUNTIME_EXPECTED_ACTIVE_MODE"], ""
        )
        self.assertEqual(
            self.environments[0]["TASKPLANNER_RUNTIME_REQUIRE_EXECUTION_IDLE"], "1"
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

    def test_live_transition_defaults_to_external_endpoint(self) -> None:
        accepted, snapshot = self.controller.start_transition("live")
        self.assertTrue(accepted)
        self.assertEqual(snapshot.phase, "starting")
        self.assertEqual(
            self.environments[0]["TASKPLANNER_LIVE_ROBOT_ENDPOINT_SOURCE"],
            "external",
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

    def test_same_mode_optional_sidecar_degradation_is_an_idle_noop(self) -> None:
        self.write_active_mode("live")
        self.controller._running_modes_probe = lambda: {"live"}
        self.controller._mode_contract_probe = lambda _mode: True
        # ASR/VLM/UI/public rosbridge health has an independent owner/restart
        # path and is not even part of the mode-controller constructor.
        self.controller._transition_interlock_probe = Mock(
            side_effect=AssertionError("sidecar degradation must not request a core stop")
        )

        accepted, snapshot = self.controller.start_transition("live")

        self.assertTrue(accepted)
        self.assertEqual(snapshot.phase, "idle")
        self.assertIn("already ready", snapshot.message)
        self.assertEqual(self.commands, [])
        self.controller._transition_interlock_probe.assert_not_called()
        controller_source = Path(runtime_control.__file__).read_text(encoding="utf-8")
        self.assertNotIn("required_plane", controller_source)

    def test_same_mode_reconciles_a_missing_split_owner_without_a_global_gate(self) -> None:
        self.write_active_mode("live")
        self.controller._running_modes_probe = lambda: {"live"}
        self.controller._mode_contract_probe = lambda _mode: True
        self.controller._transition_interlock_probe = Mock(return_value=True)
        self.controller._owner_status_probe = lambda _root, _mode: [
            {
                "owner": "core",
                "mode": "live",
                "state": "running",
                "service": "taskplanner-state-core",
                "detail": "Up 1 second",
            },
            {
                "owner": "command",
                "mode": "live",
                "state": "exited",
                "service": "taskplanner-command",
                "detail": "Exited (1)",
            },
            {
                "owner": "asr",
                "mode": "live",
                "state": "exited",
                "service": "taskplanner-asr",
                "detail": "Exited (1)",
            },
        ]

        accepted, snapshot = self.controller.start_transition("live")

        self.assertTrue(accepted)
        self.assertEqual(snapshot.phase, "starting")
        self.assertEqual(snapshot.active_mode, "live")
        self.assertIn("Starting", snapshot.message)
        self.assertEqual(
            self.commands,
            [[str(Path(self.tempdir.name) / "scripts" / "taskplanner"), "up", "live"]],
        )
        self.controller._transition_interlock_probe.assert_called_once_with("live")
        self.process.done.set()

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
        self.controller._running_modes_probe = lambda: {"live"}
        self.controller._mode_contract_probe = lambda mode: mode == "live"

        snapshot = self.controller.snapshot()

        self.assertEqual(snapshot.phase, "idle")
        self.assertEqual(snapshot.active_mode, "live")
        self.assertIsNone(snapshot.requested_mode)
        self.assertFalse(snapshot.retryable)

    def test_ready_direct_launch_supersedes_an_older_failed_mode_request(self) -> None:
        self.controller._phase = "failed"
        self.controller._requested_mode = "debug"
        self.write_active_mode("live")
        self.controller._running_modes_probe = lambda: {"live"}
        self.controller._mode_contract_probe = lambda mode: mode == "live"

        snapshot = self.controller.snapshot()

        self.assertEqual(snapshot.phase, "idle")
        self.assertEqual(snapshot.active_mode, "live")
        self.assertIsNone(snapshot.requested_mode)
        self.assertFalse(snapshot.retryable)

    def test_failed_state_stays_closed_when_marker_and_discovery_disagree(self) -> None:
        self.controller._phase = "failed"
        self.controller._requested_mode = "debug"
        self.write_active_mode("live")
        self.controller._running_modes_probe = lambda: {"debug"}
        self.controller._mode_contract_probe = lambda mode: mode == "live"

        snapshot = self.controller.snapshot()

        self.assertEqual(snapshot.phase, "failed")
        self.assertEqual(snapshot.active_mode, "live")
        self.assertEqual(snapshot.requested_mode, "debug")
        self.assertTrue(snapshot.retryable)

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

    def test_transition_interlock_scopes_unknown_activity_to_operational_routes(self) -> None:
        self.write_active_mode("replay")
        self.controller._transition_interlock_probe = lambda _mode: False
        accepted, snapshot = self.controller.start_transition("debug")
        self.assertFalse(accepted)
        self.assertEqual(snapshot.phase, "idle")
        self.assertEqual(snapshot.active_mode, "replay")
        self.assertIn("endpoint request", snapshot.message)
        self.assertEqual(self.commands, [])

        # Replay has no operational execution owner, so an unavailable probe
        # remains irrelevant to the Debug observer transition.
        self.controller._transition_interlock_probe = lambda _mode: None
        accepted, snapshot = self.controller.start_transition("debug")
        self.assertTrue(accepted)
        self.assertEqual(snapshot.phase, "starting")
        self.process.done.set()

    def test_transition_rejects_unknown_activity_for_an_operational_route(self) -> None:
        self.write_active_mode("live")
        self.controller._running_modes_probe = lambda: {"live"}
        self.controller._transition_interlock_probe = lambda _mode: None

        accepted, snapshot = self.controller.start_transition("debug")

        self.assertFalse(accepted)
        self.assertEqual(snapshot.phase, "idle")
        self.assertEqual(snapshot.active_mode, "live")
        self.assertIn("activity is unavailable", snapshot.message)
        self.assertEqual(self.commands, [])

    def test_http_transition_returns_conflict_for_active_endpoint_request(self) -> None:
        self.write_active_mode("replay")
        self.controller._transition_interlock_probe = lambda _mode: False
        with self.assertRaises(HTTPError) as context:
            self.request("POST", "/v1/runtime/transition", {"mode": "debug"})
        self.assertEqual(context.exception.code, 409)
        context.exception.close()
        self.assertEqual(self.commands, [])

    def test_http_transition_accepts_idle_execution_owner(self) -> None:
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

    def test_transition_accepts_idle_execution_owner(self) -> None:
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


class SurgiMateControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_file = self.root / "active-runtime-mode.json"
        self.state_file.write_text('{"mode":"debug"}', encoding="utf-8")
        self.commands: list[list[str]] = []

        def command_runner(command: list[str], **_kwargs: object):
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        self.rows = [
            {
                "owner": "debug-observer",
                "mode": "debug",
                "state": "running",
                "service": "taskplanner-debug-observer",
                "detail": "Up 2 seconds",
            },
            {
                "owner": "surgimate",
                "mode": "debug",
                "state": "exited",
                "service": "taskplanner-surgimate",
                "detail": "Exited (0)",
            },
        ]
        self.controller = runtime_control.RuntimeController(
            root=self.root,
            state_file=self.state_file,
            launcher=self.root / "scripts" / "taskplanner",
            mode_running_probe=lambda _mode: True,
            mode_contract_probe=lambda _mode: True,
            running_modes_probe=lambda: {"debug"},
            owner_status_probe=lambda _root, _mode: self.rows,
            command_runner=command_runner,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_fixed_debug_cli_controls_only_the_surgimate_sidecar(self) -> None:
        accepted, message = self.controller.control_surgimate("restart")

        self.assertTrue(accepted)
        self.assertEqual(message, "SurgiMate restarted.")
        self.assertEqual(
            self.commands,
            [[
                str(self.root / "scripts" / "taskplanner"),
                "surgimate",
                "restart",
                "--mode",
                "debug",
            ]],
        )
        self.assertFalse(any(command[0] == "docker" for command in self.commands))

    def test_live_allows_only_the_scoped_surgimate_restart(self) -> None:
        self.state_file.write_text('{"mode":"live"}', encoding="utf-8")
        self.controller._running_modes_probe = lambda: {"live"}

        accepted, message = self.controller.control_surgimate("start")
        self.assertFalse(accepted)
        self.assertIn("only be restarted", message)
        self.assertEqual(self.commands, [])

        accepted, message = self.controller.control_surgimate("restart")
        self.assertTrue(accepted)
        self.assertEqual(message, "SurgiMate restarted.")
        self.assertEqual(
            self.commands,
            [[
                str(self.root / "scripts" / "taskplanner"),
                "restart",
                "surgimate",
                "live",
            ]],
        )
        with self.assertRaises(ValueError):
            self.controller.control_surgimate("shell")

    def test_status_uses_the_active_live_sidecar_projection(self) -> None:
        self.state_file.write_text('{"mode":"live"}', encoding="utf-8")
        self.controller._running_modes_probe = lambda: {"live"}
        live_row = {
            "owner": "surgimate",
            "mode": "live",
            "state": "running",
            "service": "taskplanner-surgimate",
            "detail": "Up 2 seconds",
        }
        self.controller._owner_status_probe = lambda _root, mode: (
            [live_row] if mode == "live" else self.rows
        )

        self.assertEqual(self.controller.surgimate_status(), live_row)

    def test_sidecar_is_excluded_from_generic_owner_actions_and_readiness(self) -> None:
        self.assertTrue(runtime_control.runtime_owner_plane_ready(self.rows, "debug"))
        generic_rows = self.controller.owner_status("debug")
        self.assertEqual([row["owner"] for row in generic_rows], ["debug-observer"])
        with self.assertRaises(ValueError):
            self.controller.restart_owner("surgimate", "debug")

    def test_generic_owner_status_omits_non_restartable_mode_rows(self) -> None:
        rows = [
            *self.rows,
            {
                "owner": "command",
                "mode": "debug",
                "state": "not-applicable",
                "service": "",
                "detail": "mode not supported",
            },
            {
                "owner": "debug-virtual",
                "mode": "debug",
                "state": "disabled",
                "service": "taskplanner-debug-virtual",
                "detail": "disabled by TASKPLANNER_DEBUG_ENABLE_VIRTUAL_ROBOT",
            },
        ]
        self.controller._owner_status_probe = lambda _root, _mode: rows

        self.assertEqual(
            [row["owner"] for row in self.controller.owner_status("debug")],
            ["debug-observer"],
        )

    def test_disabled_optional_owner_is_not_a_runtime_readiness_failure(self) -> None:
        rows = [
            *self.rows,
            {
                "owner": "debug-virtual",
                "mode": "debug",
                "state": "disabled",
                "service": "taskplanner-debug-virtual",
                "detail": "disabled by TASKPLANNER_DEBUG_ENABLE_VIRTUAL_ROBOT",
            },
        ]
        self.assertTrue(runtime_control.runtime_owner_plane_ready(rows, "debug"))


class TtsOwnerControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_file = self.root / "active-runtime-mode.json"
        self.state_file.write_text('{"mode":"live"}', encoding="utf-8")
        self.commands: list[list[str]] = []

        def command_runner(command: list[str], **_kwargs: object):
            self.commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        self.rows = [
            {
                "owner": "tts",
                "mode": "live",
                "state": "running",
                "service": "taskplanner-tts",
                "detail": "Up 2 seconds (healthy)",
            },
            {
                "owner": "surgimate",
                "mode": "live",
                "state": "running",
                "service": "taskplanner-surgimate",
                "detail": "Up 2 seconds",
            },
        ]
        self.controller = runtime_control.RuntimeController(
            root=self.root,
            state_file=self.state_file,
            launcher=self.root / "scripts" / "taskplanner",
            mode_running_probe=lambda _mode: True,
            mode_contract_probe=lambda _mode: True,
            running_modes_probe=lambda: {"live"},
            owner_status_probe=lambda _root, _mode: self.rows,
            command_runner=command_runner,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_tts_uses_generic_owner_status_and_scoped_restart(self) -> None:
        self.assertEqual(
            [row["owner"] for row in self.controller.owner_status("live")],
            ["tts"],
        )

        accepted, message = self.controller.restart_owner("tts", "live")

        self.assertTrue(accepted)
        self.assertEqual(message, "The owner restarted.")
        self.assertEqual(
            self.commands,
            [[
                str(self.root / "scripts" / "taskplanner"),
                "restart",
                "tts",
                "live",
            ]],
        )


class RuntimeStateInterlockTests(unittest.TestCase):
    def test_final_gate_keeps_single_runtime_identity_and_fails_closed_for_active_route_unknowns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            state_file = root / "active-runtime-mode.json"
            state_file.write_text('{"mode":"live"}', encoding="utf-8")

            safe, reason = runtime_control.final_transition_interlock_is_safe(
                root,
                state_file,
                "live",
                running_modes_probe=lambda: {"live"},
                execution_idle_probe=lambda _mode: True,
            )
            self.assertTrue(safe)
            self.assertIn("no execution endpoint request", reason)

            safe, reason = runtime_control.final_transition_interlock_is_safe(
                root,
                state_file,
                "live",
                running_modes_probe=lambda: {"live"},
                execution_idle_probe=lambda _mode: False,
            )
            self.assertFalse(safe)
            self.assertIn("in flight", reason)

            # The affected Live execution owner is unknown, so route
            # replacement must fail closed without involving any other owner.
            safe, reason = runtime_control.final_transition_interlock_is_safe(
                root,
                state_file,
                "live",
                running_modes_probe=lambda: {"live"},
                execution_idle_probe=lambda _mode: None,
            )
            self.assertFalse(safe)
            self.assertIn("activity is unavailable", reason)

            # A stopped state-core anchor does not prove that its separate
            # execution owner is idle. Keep the same targeted failure mode
            # until that owner can report its own state.
            safe, reason = runtime_control.final_transition_interlock_is_safe(
                root,
                state_file,
                "live",
                running_modes_probe=lambda: set(),
                execution_idle_probe=lambda _mode: None,
            )
            self.assertFalse(safe)
            self.assertIn("activity is unavailable", reason)

            # Replay has no operational execution owner. An unavailable probe
            # there is not a global readiness barrier.
            state_file.write_text('{"mode":"replay"}', encoding="utf-8")
            safe, reason = runtime_control.final_transition_interlock_is_safe(
                root,
                state_file,
                "replay",
                running_modes_probe=lambda: {"replay"},
                execution_idle_probe=lambda _mode: None,
            )
            self.assertTrue(safe)
            self.assertIn("no operational execution endpoint owner", reason)

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
                execution_idle_probe=lambda _mode: True,
            )
            self.assertFalse(safe)

    def test_execution_route_state_parser_uses_only_active_request_facts(self) -> None:
        def sample(active_count: int, proxy_active: bool, **extra: object) -> dict[str, str]:
            return {
                "data": json.dumps(
                    {
                        "schema": runtime_control.EXECUTION_ROUTE_STATE_SCHEMA,
                        "active_request_count": active_count,
                        "execution_proxy_active": proxy_active,
                        # Deliberately contradictory legacy convenience fields:
                        # neither is a mode-transition precondition now.
                        "restart_allowed": False,
                        "restart_blocker": "simulation_not_stopped",
                        **extra,
                    }
                )
            }

        self.assertTrue(runtime_control.execution_route_state_is_idle(sample(0, False)))
        self.assertFalse(runtime_control.execution_route_state_is_idle(sample(1, False)))
        self.assertFalse(runtime_control.execution_route_state_is_idle(sample(0, True)))
        self.assertIsNone(runtime_control.execution_route_state_is_idle({"data": "{}"}))
        self.assertIsNone(
            runtime_control.execution_route_state_is_idle(
                sample(-1, False)
            )
        )

    def test_execution_owner_probe_reads_latched_route_state_not_simulation_trigger(self) -> None:
        topic_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "data: '{\"schema\":\"taskplanner.execution_route_state.v1\","
                "\"active_request_count\":1,\"execution_proxy_active\":false}'\n"
            ),
            stderr="",
        )
        with patch.object(
            runtime_control,
            "_running_owner_container_id",
            return_value="execution-container",
        ), patch.object(
            runtime_control.subprocess,
            "run",
            return_value=topic_result,
        ) as run:
            self.assertFalse(runtime_control.execution_owner_is_idle(Path("/workspace"), "live"))
        command = run.call_args.args[0]
        self.assertIn("ros2 topic echo", command[5])
        self.assertIn(runtime_control.EXECUTION_ROUTE_STATE_TOPIC, command)
        self.assertIn(runtime_control.EXECUTION_ROUTE_STATE_TYPE, command)
        self.assertNotIn("ros2 service call", command[5])
        self.assertNotIn("check_transition_ready", command)
        self.assertEqual(run.call_args.kwargs["timeout"], 3.0)

    def test_execution_owner_observation_is_not_required_for_non_operational_modes(self) -> None:
        self.assertTrue(runtime_control.execution_owner_is_idle(Path("/workspace"), "replay"))
        self.assertTrue(runtime_control.execution_owner_is_idle(Path("/workspace"), "debug"))
        with patch.object(
            runtime_control,
            "_running_owner_container_id",
            return_value=None,
        ):
            self.assertIsNone(runtime_control.execution_owner_is_idle(Path("/workspace"), "live"))


class RosbridgeRouteProbeTests(unittest.TestCase):
    def test_live_core_probe_uses_declared_loopback_bridge_not_optional_router(self) -> None:
        root = Path("/workspace")
        with patch.object(
            runtime_control,
            "_running_service_container_id",
            return_value="operator-bridge-container",
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


class AsrRestartOwnerTests(unittest.TestCase):
    """The dashboard must be only a client of the CLI ASR owner command."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_file = self.root / "active-runtime-mode.json"
        self.state_file.write_text('{"mode":"live"}', encoding="utf-8")
        self.commands: list[list[str]] = []
        self.returncode = 0
        self.started = threading.Event()
        self.release: threading.Event | None = None

        def command_runner(command: list[str], **_kwargs: object):
            self.commands.append(command)
            self.started.set()
            if self.release is not None:
                self.release.wait(timeout=2)
            return subprocess.CompletedProcess(
                command,
                self.returncode,
                "",
                "owner restart failed" if self.returncode else "",
            )

        self.controller = runtime_control.RuntimeController(
            root=self.root,
            state_file=self.state_file,
            launcher=self.root / "scripts" / "taskplanner",
            mode_running_probe=lambda _mode: True,
            mode_contract_probe=lambda _mode: True,
            running_modes_probe=lambda: {"live"},
            command_runner=command_runner,
            asr_restart_command_timeout_sec=1,
        )

    def tearDown(self) -> None:
        if self.release is not None:
            self.release.set()
        self.tempdir.cleanup()

    def wait_for_terminal(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshot = self.controller.asr_restart_snapshot()
            if snapshot.phase in {"succeeded", "failed"}:
                return snapshot
            time.sleep(0.005)
        self.fail("ASR owner restart did not reach a terminal state")

    def test_dashboard_uses_the_exact_cli_owner_restart(self) -> None:
        accepted, queued = self.controller.start_asr_restart(
            "11111111-1111-4111-8111-111111111111"
        )

        self.assertTrue(accepted)
        self.assertEqual(queued.phase, "queued")
        snapshot = self.wait_for_terminal()

        self.assertEqual(snapshot.phase, "succeeded")
        self.assertEqual(snapshot.message, "The ASR owner restarted.")
        self.assertIsNone(snapshot.source_revision)
        self.assertIsNone(snapshot.container_started_at)
        self.assertIsNone(snapshot.before_pid)
        self.assertIsNone(snapshot.after_pid)
        self.assertEqual(
            self.commands,
            [[
                str(self.root / "scripts" / "taskplanner"),
                "restart",
                "asr",
                "--require-active-live",
            ]],
        )
        self.assertFalse(any(command[0] == "docker" for command in self.commands))

    def test_owner_failure_is_reported_without_a_second_restart_path(self) -> None:
        self.returncode = 7

        accepted, _queued = self.controller.start_asr_restart()

        self.assertTrue(accepted)
        snapshot = self.wait_for_terminal()
        self.assertEqual(snapshot.phase, "failed")
        self.assertTrue(snapshot.retryable)
        self.assertEqual(snapshot.message, "The ASR owner restart failed.")
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][1:3], ["restart", "asr"])

    def test_asr_restart_serializes_mode_changes_without_polling_sidecars(self) -> None:
        self.release = threading.Event()
        accepted, _queued = self.controller.start_asr_restart()
        self.assertTrue(accepted)
        self.assertTrue(self.started.wait(timeout=1))

        accepted_transition, snapshot = self.controller.start_transition("replay")

        self.assertFalse(accepted_transition)
        self.assertIn("ASR node restart", snapshot.message)
        self.assertEqual(len(self.commands), 1)
        self.release.set()
        self.assertEqual(self.wait_for_terminal().phase, "succeeded")


class RuntimeLifecycleTests(unittest.TestCase):
    """Fixed Debug lifecycle controls stay separate from generic owner APIs."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_file = self.root / "active-runtime-mode.json"
        self.state_file.write_text('{"mode":"live"}', encoding="utf-8")
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

        def command_runner(command: list[str], **kwargs: object):
            self.commands.append(command)
            environment = kwargs.get("env")
            if isinstance(environment, dict):
                self.environments.append(dict(environment))
            return subprocess.CompletedProcess(command, 0, "", "")

        self.controller = runtime_control.RuntimeController(
            root=self.root,
            state_file=self.state_file,
            launcher=self.root / "scripts" / "taskplanner",
            mode_running_probe=lambda _mode: True,
            mode_contract_probe=lambda _mode: True,
            running_modes_probe=lambda: {"live"},
            transition_interlock_probe=lambda _mode: True,
            command_runner=command_runner,
        )
        self.controller._ninfer_snapshot = lambda: runtime_control.NInferSnapshot(
            available=True,
            model_id="qwen3.6-35b-a3b",
            model_state="loaded",
            detail="ready",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def wait_for_terminal(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self.controller._lock:
                snapshot = self.controller._lifecycle_snapshot
            if snapshot.phase in {"succeeded", "failed"}:
                return snapshot
            time.sleep(0.005)
        self.fail("runtime lifecycle did not reach a terminal state")

    def test_warm_restart_uses_the_existing_same_mode_launcher_path(self) -> None:
        accepted, queued = self.controller.start_lifecycle(
            "warm_restart", "22222222-2222-4222-8222-222222222222"
        )

        self.assertTrue(accepted)
        self.assertEqual(queued.phase, "queued")
        self.assertEqual(self.wait_for_terminal().phase, "succeeded")
        self.assertEqual(
            self.commands,
            [[str(self.root / "scripts" / "taskplanner"), "up", "live"]],
        )
        self.assertEqual(self.environments[0]["TASKPLANNER_RUNTIME_CONTROL_CHILD"], "1")
        self.assertEqual(
            self.environments[0]["TASKPLANNER_RUNTIME_EXPECTED_ACTIVE_MODE"], "live"
        )

    def test_clean_restart_keeps_the_supervisor_child_contract(self) -> None:
        accepted, _queued = self.controller.start_lifecycle(
            "clean_restart", "33333333-3333-4333-8333-333333333333"
        )

        self.assertTrue(accepted)
        self.assertEqual(self.wait_for_terminal().phase, "succeeded")
        self.assertEqual(
            self.commands,
            [
                [str(self.root / "scripts" / "taskplanner"), "down"],
                [str(self.root / "scripts" / "taskplanner"), "up", "live"],
            ],
        )
        self.assertTrue(all(
            environment["TASKPLANNER_RUNTIME_CONTROL_CHILD"] == "1"
            for environment in self.environments
        ))

    def test_lifecycle_rejects_an_active_execution_request(self) -> None:
        self.controller._transition_interlock_probe = lambda _mode: False

        accepted, snapshot = self.controller.start_lifecycle(
            "clean_restart", "44444444-4444-4444-8444-444444444444"
        )

        self.assertFalse(accepted)
        self.assertEqual(snapshot.phase, "failed")
        self.assertIn("in flight", snapshot.message)
        self.assertEqual(self.commands, [])

    def test_lifecycle_rejects_unknown_activity_for_live_restart(self) -> None:
        self.controller._transition_interlock_probe = lambda _mode: None

        accepted, snapshot = self.controller.start_lifecycle(
            "warm_restart", "55555555-5555-4555-8555-555555555555"
        )

        self.assertFalse(accepted)
        self.assertEqual(snapshot.phase, "failed")
        self.assertIn("activity is unavailable", snapshot.message)
        self.assertEqual(self.commands, [])


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
