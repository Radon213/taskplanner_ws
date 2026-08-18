#!/usr/bin/env python3
"""Loopback-only, allowlisted Taskplanner runtime-mode controller.

The dashboard never receives shell access.  It can request one of the four
reviewed runtime profiles through a Vite reverse proxy that adds the local
control token.  This process in turn invokes the existing launcher with a
fixed argv and publishes only coarse, non-sensitive transition state.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Final

import yaml


ALLOWED_MODES: Final[frozenset[str]] = frozenset({"live", "llm-surgeon", "replay", "debug"})
TOKEN_HEADER: Final[str] = "X-Taskplanner-Runtime-Control-Token"
MAX_REQUEST_BYTES: Final[int] = 4096
DEFAULT_TRANSITION_TIMEOUT_SEC: Final[float] = 420.0
TERMINATE_GRACE_SEC: Final[float] = 10.0
ACTIVE_MODE_PROBE_TTL_SEC: Final[float] = 2.0
ACTIVE_MODE_PROBE_FAILURE_THRESHOLD: Final[int] = 2
TRANSITION_READY_SERVICE: Final[str] = "/simulation/check_transition_ready"
TRANSITION_RESERVE_SERVICE: Final[str] = "/simulation/reserve_transition"
TRANSITION_READY_SERVICE_TYPE: Final[str] = "std_srvs/srv/Trigger"
TRANSITION_PROTOCOL_MARKER: Final[str] = (
    "transition-reservation-v2; dt_receipt_max_age=3.0;"
)


def source_code_fingerprint() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        return "unavailable"


LOADED_CODE_FINGERPRINT: Final[str] = source_code_fingerprint()


@dataclass
class TransitionSnapshot:
    phase: str
    active_mode: str | None
    requested_mode: str | None
    message: str
    retryable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_active_mode(state_file: Path) -> str | None:
    """Read only the launcher-written, allowlisted mode marker."""

    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    mode = payload.get("mode") if isinstance(payload, dict) else None
    return mode if mode in ALLOWED_MODES else None


def compose_service_running(
    root: Path,
    mode: str,
    *,
    router_port: int = 9091,
    route_paths: dict[str, str] | None = None,
) -> bool | None:
    """Cheaply reconcile the marker with the mode's required Compose service."""

    service = {
        "live": "taskplanner-runtime",
        "llm-surgeon": "taskplanner-runtime",
        "replay": "shadow-runner",
        "debug": "integration-debug",
    }.get(mode)
    if service is None:
        return False
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project.working_dir={root.resolve()}",
                "--filter",
                f"label=com.docker.compose.service={service}",
                "--filter",
                "status=running",
                "--format",
                "{{.ID}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    if not result.stdout.strip():
        return False
    return websocket_route_ready(mode, port=router_port, route_paths=route_paths)


def _running_mode_container_id(root: Path, mode: str) -> str | None:
    service = {
        "live": "taskplanner-runtime",
        "llm-surgeon": "taskplanner-runtime",
        "replay": "shadow-runner",
        "debug": "integration-debug",
    }.get(mode)
    if service is None:
        return None
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project.working_dir={root.resolve()}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=1.0,
    )
    if result.returncode != 0:
        return None
    identifiers = result.stdout.split()
    return identifiers[0] if len(identifiers) == 1 else None


def detect_running_mode_candidates(root: Path) -> set[str] | None:
    """Detect running core runtimes even when the active marker is missing."""

    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project.working_dir={root.resolve()}",
                "--filter",
                "status=running",
                "--format",
                "{{.Label \"com.docker.compose.service\"}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    services = set(result.stdout.split())
    candidates: set[str] = set()
    if "taskplanner-runtime" in services:
        # Live and LLM use the same safety-relevant SimulationState contract.
        candidates.add("live")
    if "shadow-runner" in services:
        candidates.add("replay")
    # Integrated Debug may legitimately coexist with the operational runtime;
    # only treat it as the active core when no operational/replay core exists.
    if "integration-debug" in services and not candidates:
        candidates.add("debug")
    return candidates


def final_transition_interlock_is_safe(
    root: Path,
    state_file: Path,
    expected_mode: str | None,
    *,
    running_modes_probe: Callable[[], set[str] | None] | None = None,
    inactive_probe: Callable[[str], bool | None] | None = None,
    reservation_probe: Callable[[str], bool | None] | None = None,
) -> tuple[bool, str]:
    """Revalidate a controller-authorized transition after the launcher lock."""

    if expected_mode is not None and expected_mode not in ALLOWED_MODES:
        return False, "the expected runtime mode is invalid"
    marker_mode = read_active_mode(state_file)
    try:
        candidates = (
            running_modes_probe()
            if running_modes_probe is not None
            else detect_running_mode_candidates(root)
        )
    except Exception:
        candidates = None
    if candidates is None:
        return False, "running runtime detection is unavailable"

    if expected_mode is None:
        if marker_mode is not None or candidates:
            return False, "a runtime appeared after the controller safety check"
        return True, "no active runtime remains"

    if marker_mode not in {None, expected_mode}:
        return False, "the active runtime marker changed during the transition"

    # Live and LLM Surgeon share the taskplanner-runtime container and fresh
    # SimulationState contract. Candidate discovery reports that service as
    # live when no authoritative marker is available.
    expected_candidate = (
        "live" if expected_mode in {"live", "llm-surgeon"} else expected_mode
    )
    if candidates - {expected_candidate}:
        return False, "a different or additional runtime is now running"
    if not candidates:
        return True, "the previously active runtime has already stopped"
    if expected_candidate not in candidates:
        return False, "the expected runtime could not be identified"

    if expected_mode in {"live", "llm-surgeon"}:
        try:
            reserved = (
                reservation_probe(expected_mode)
                if reservation_probe is not None
                else reserve_mode_transition(root, expected_mode)
            )
        except Exception:
            reserved = None
        if reserved is not True:
            return False, "the active runtime transition could not be reserved"
        return True, "the active runtime is stopped and transition-reserved"

    try:
        inactive = (
            inactive_probe(expected_mode)
            if inactive_probe is not None
            else probe_mode_inactive(root, expected_mode)
        )
    except Exception:
        inactive = None
    if inactive is True:
        return True, "the active runtime remains freshly stopped"
    if inactive is False:
        return False, "the active runtime started or paused after the controller safety check"
    return False, "the active runtime state is no longer verifiable"


def mode_state_is_inactive(mode: str, payload: dict[str, Any]) -> bool:
    """Return whether a fresh runtime state is safe to replace."""

    if mode in {"live", "llm-surgeon"}:
        running = payload.get("running")
        execution_state = str(payload.get("execution_state", "")).strip().lower()
        return running is False and execution_state in {"idle", "halted", "completed"}
    if mode == "replay":
        state = str(payload.get("state", "")).strip().lower()
        running = payload.get("running")
        paused = payload.get("paused")
        return (
            running is False
            and paused is False
            and state
            in {"ready", "stopped", "completed", "timed_out", "blocked", "error"}
        )
    if mode == "debug":
        raw = payload.get("data")
        if not isinstance(raw, str):
            return False
        try:
            status = json.loads(raw)
        except ValueError:
            return False
        session = status.get("session") if isinstance(status, dict) else None
        return (
            isinstance(session, dict)
            and session.get("state") == "MONITOR_ONLY"
            and session.get("armed") is False
        )
    return False


def trigger_response_success(output: str) -> bool | None:
    match = re.search(r"\bsuccess\s*(?:=|:)\s*(True|False|true|false)\b", output)
    if match is None:
        return None
    return match.group(1).lower() == "true"


def trigger_response_message(output: str) -> str | None:
    quoted = re.search(
        r"\bmessage\s*=\s*(?P<quote>['\"])(?P<message>.*?)(?P=quote)",
        output,
        flags=re.DOTALL,
    )
    if quoted is not None:
        return quoted.group("message")
    yaml_line = re.search(r"^\s*message\s*:\s*(.*?)\s*$", output, flags=re.MULTILINE)
    if yaml_line is None:
        return None
    return yaml_line.group(1).strip("'\"")


def _call_operational_trigger(
    container_id: str,
    service_name: str,
) -> bool | None:
    try:
        result = subprocess.run(
            [
                "docker",
                "exec",
                container_id,
                "bash",
                "-lc",
                "source /opt/ros/jazzy/setup.bash; "
                "source /opt/btops_ws/install/setup.bash; "
                "source /workspaces/taskplanner_ws/install/setup.bash; "
                "timeout 4 ros2 service call \"$1\" \"$2\" '{}'",
                "--",
                service_name,
                TRANSITION_READY_SERVICE_TYPE,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=6.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    success = trigger_response_success(result.stdout)
    if success is not True:
        return success
    # A boolean-only response from an old in-memory manager did not enforce the
    # receipt-freshness contract.  Require the protocol marker fail-closed.
    message = trigger_response_message(result.stdout)
    return bool(message and message.startswith(TRANSITION_PROTOCOL_MARKER))


def _probe_operational_transition_ready(container_id: str) -> bool | None:
    return _call_operational_trigger(container_id, TRANSITION_READY_SERVICE)


def reserve_mode_transition(root: Path, mode: str) -> bool | None:
    """Atomically reserve a verified inactive operational runtime."""

    if mode not in {"live", "llm-surgeon"}:
        return True
    container_id = _running_mode_container_id(root, mode)
    if container_id is None:
        return None
    return _call_operational_trigger(container_id, TRANSITION_RESERVE_SERVICE)


def probe_mode_inactive(root: Path, mode: str) -> bool | None:
    """Read one fresh ROS state sample from the active runtime container."""

    try:
        container_id = _running_mode_container_id(root, mode)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if container_id is None:
        return None
    if mode in {"live", "llm-surgeon"}:
        # The manager's typed Trigger performs the fresh SimulationState, local
        # operation, and executor checks as one authoritative contract.
        return _probe_operational_transition_ready(container_id)

    topic_contract = {
        "replay": ("/shadow/replay_state", "surgical_msgs/msg/ShadowReplayState"),
        "debug": ("/integration/debug/status", "std_msgs/msg/String"),
    }.get(mode)
    if topic_contract is None:
        return None
    try:
        topic, message_type = topic_contract
        result = subprocess.run(
            [
                "docker",
                "exec",
                container_id,
                "bash",
                "-lc",
                "source /opt/ros/jazzy/setup.bash; "
                "source /opt/btops_ws/install/setup.bash; "
                "source /workspaces/taskplanner_ws/install/setup.bash; "
                "timeout 6 ros2 topic echo --once --no-daemon --spin-time 1 "
                "--timeout 4 --flow-style --full-length \"$1\" \"$2\"",
                "--",
                topic,
                message_type,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = next(
            (
                document
                for document in yaml.safe_load_all(result.stdout)
                if document is not None
            ),
            None,
        )
    except yaml.YAMLError:
        return None
    if not isinstance(payload, dict):
        return None
    inactive = mode_state_is_inactive(mode, payload)
    if not inactive:
        return False
    return True


def websocket_route_ready(
    mode: str,
    *,
    port: int = 9091,
    route_paths: dict[str, str] | None = None,
) -> bool:
    paths = route_paths or {
        "live": "/live",
        "llm-surgeon": "/llm",
        "replay": "/shadow",
        "debug": "/",
    }
    path = paths.get(mode)
    if path is None:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as connection:
            connection.settimeout(0.5)
            connection.sendall(
                (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode("ascii")
            )
            response = connection.recv(256)
    except OSError:
        return False
    return response.startswith((b"HTTP/1.1 101 ", b"HTTP/1.0 101 "))


class RuntimeController:
    """Serialize fixed launcher invocations and retain coarse job state."""

    def __init__(
        self,
        *,
        root: Path,
        state_file: Path,
        launcher: Path | None = None,
        launcher_log_file: Path | None = None,
        popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
        transition_timeout_sec: float = DEFAULT_TRANSITION_TIMEOUT_SEC,
        mode_running_probe: Callable[[str], bool | None] | None = None,
        active_probe_ttl_sec: float = ACTIVE_MODE_PROBE_TTL_SEC,
        active_probe_failure_threshold: int = ACTIVE_MODE_PROBE_FAILURE_THRESHOLD,
        transition_interlock_probe: Callable[[str], bool | None] | None = None,
        router_port: int = 9091,
        route_paths: dict[str, str] | None = None,
        running_modes_probe: Callable[[], set[str] | None] | None = None,
    ) -> None:
        if transition_timeout_sec <= 0:
            raise ValueError("transition timeout must be positive")
        if active_probe_ttl_sec < 0:
            raise ValueError("active probe TTL must not be negative")
        if active_probe_failure_threshold < 1:
            raise ValueError("active probe failure threshold must be positive")
        self._root = root.resolve()
        self._state_file = state_file
        self._launcher = launcher or self._root / "scripts" / "taskplanner"
        self._launcher_log_file = launcher_log_file or state_file.with_name("runtime-control-launch.log")
        self._popen_factory = popen_factory
        self._transition_timeout_sec = float(transition_timeout_sec)
        self._mode_running_probe = mode_running_probe or (
            lambda mode: compose_service_running(
                self._root,
                mode,
                router_port=router_port,
                route_paths=route_paths,
            )
        )
        self._active_probe_ttl_sec = float(active_probe_ttl_sec)
        self._active_probe_failure_threshold = int(active_probe_failure_threshold)
        self._transition_interlock_probe = transition_interlock_probe or (
            lambda mode: probe_mode_inactive(self._root, mode)
        )
        self._running_modes_probe = running_modes_probe or (
            lambda: detect_running_mode_candidates(self._root)
        )
        self._last_probe_at = 0.0
        self._last_probe_mode: str | None = None
        self._last_probe_running: bool | None = None
        self._consecutive_probe_failures = 0
        self._lock = threading.RLock()
        self._phase = "idle"
        self._requested_mode: str | None = None
        self._message = "Runtime mode control is ready."
        self._process: Any | None = None
        self._output: Any | None = None

    def snapshot(self) -> TransitionSnapshot:
        with self._lock:
            # The launcher's atomic marker is the only authoritative mode. A
            # cached success must never survive a later partial stop/failure.
            active_mode = self._reconcile_active_mode_locked()
            return TransitionSnapshot(
                phase=self._phase,
                active_mode=active_mode,
                requested_mode=self._requested_mode,
                message=self._message,
                retryable=self._phase == "failed",
            )

    def _reconcile_active_mode_locked(self) -> str | None:
        active_mode = read_active_mode(self._state_file)
        if self._phase != "idle" or active_mode is None:
            return active_mode
        now = time.monotonic()
        fresh_probe = False
        if (
            self._last_probe_mode == active_mode
            and now - self._last_probe_at <= self._active_probe_ttl_sec
        ):
            running = self._last_probe_running
        else:
            fresh_probe = True
            try:
                running = self._mode_running_probe(active_mode)
            except Exception:
                running = None
            self._last_probe_at = now
            self._last_probe_mode = active_mode
            self._last_probe_running = running
        if running is True:
            self._consecutive_probe_failures = 0
        elif running is False and fresh_probe:
            self._consecutive_probe_failures += 1
        if (
            running is False
            and self._consecutive_probe_failures >= self._active_probe_failure_threshold
        ):
            self._invalidate_active_mode_marker()
            self._phase = "failed"
            self._requested_mode = active_mode
            self._message = "Active runtime stopped unexpectedly. Select a mode to restart."
            return None
        return active_mode

    def start_transition(self, mode: str) -> tuple[bool, TransitionSnapshot]:
        if mode not in ALLOWED_MODES:
            raise ValueError("unsupported runtime mode")

        with self._lock:
            if self._phase == "starting":
                return False, self.snapshot()

            active_mode = self._reconcile_active_mode_locked()
            if active_mode is None:
                try:
                    candidates = self._running_modes_probe()
                except Exception:
                    candidates = None
                if candidates is None or len(candidates) > 1:
                    self._requested_mode = mode
                    self._message = (
                        "Could not determine a single active runtime. "
                        "Stop all runtimes before switching modes."
                    )
                    return False, self.snapshot()
                if len(candidates) == 1:
                    active_mode = next(iter(candidates))
            if active_mode is not None:
                try:
                    inactive = self._transition_interlock_probe(active_mode)
                except Exception:
                    inactive = None
                if inactive is not True:
                    self._requested_mode = mode
                    if inactive is False:
                        self._message = (
                            "Stop the active runtime before switching modes."
                        )
                    else:
                        self._message = (
                            "Could not verify that the active runtime is stopped. "
                            "Retry after its state becomes available."
                        )
                    return False, self.snapshot()

            self._phase = "starting"
            self._requested_mode = mode
            self._message = "Starting the selected runtime."
            self._last_probe_at = 0.0
            self._last_probe_mode = None
            self._last_probe_running = None
            self._consecutive_probe_failures = 0
            self._launcher_log_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._output = self._launcher_log_file.open("ab", buffering=0)
                environment = os.environ.copy()
                # A terminal/session override must not make a mode transition
                # inherit a stale DDS configuration from an earlier launch.
                environment.pop("CYCLONEDDS_URI", None)
                # The launcher must never stop/restart the controller that is
                # currently supervising it merely because the source changed.
                environment["TASKPLANNER_RUNTIME_CONTROL_CHILD"] = "1"
                # Recheck the same active runtime after the child takes the
                # launcher lock, immediately before marker clear/stop.
                environment["TASKPLANNER_RUNTIME_REQUIRE_STOPPED"] = "1"
                environment["TASKPLANNER_RUNTIME_EXPECTED_ACTIVE_MODE"] = (
                    active_mode or ""
                )
                command = [str(self._launcher), "up", mode, "--no-build"]
                # Standalone Debug normally refuses to replace an operational
                # runtime from an arbitrary terminal.  This service is reached
                # only through the reviewed dashboard transition flow, so the
                # explicit user mode selection is carried through safely.
                if mode == "debug":
                    command.append("--replace-active")
                self._process = self._popen_factory(
                    command,
                    cwd=str(self._root),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=self._output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError:
                if self._output is not None:
                    self._output.close()
                    self._output = None
                self._process = None
                self._phase = "failed"
                self._message = "Could not start the runtime launcher. Try again."
                return False, self.snapshot()

            process = self._process
            threading.Thread(
                target=self._wait_for_transition,
                args=(process, mode),
                daemon=True,
                name="taskplanner-runtime-transition",
            ).start()
            return True, self.snapshot()

    def _wait_for_transition(self, process: Any, mode: str) -> None:
        timed_out = False
        try:
            return_code = process.wait(timeout=self._transition_timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_process_group(process)
            return_code = None
        with self._lock:
            if process is not self._process:
                return
            if self._output is not None:
                self._output.close()
                self._output = None
            self._process = None
            active_mode = read_active_mode(self._state_file)
            if return_code == 0 and active_mode == mode:
                self._phase = "idle"
                self._requested_mode = None
                self._message = "Selected runtime is ready."
            else:
                # The launcher deliberately preserves the previous marker when
                # its final under-lock transition interlock rejects a TOCTOU
                # state change. Keep that still-valid runtime visible. If the
                # launcher already crossed the safety boundary it atomically
                # cleared the marker before stopping services, so active_mode
                # is naturally unknown and must remain null.
                self._phase = "failed"
                if timed_out:
                    self._message = "Runtime startup timed out. Review the host log and retry."
                elif return_code == 0:
                    self._message = "Runtime startup did not publish a ready mode. Review the host log and retry."
                else:
                    self._message = "Runtime startup failed. Review the host log and retry."
        if (
            os.environ.get("INVOCATION_ID")
            and source_code_fingerprint() != LOADED_CODE_FINGERPRINT
        ):
            # systemd Restart=on-failure reloads the updated source after the
            # supervised launcher has fully completed. This avoids killing a
            # transition from its own child while still converging an UI-only
            # deployment to the new controller fingerprint.
            threading.Thread(
                target=self._exit_for_code_reload,
                daemon=True,
                name="taskplanner-runtime-control-reload",
            ).start()

    @staticmethod
    def _exit_for_code_reload() -> None:
        time.sleep(0.5)
        os._exit(75)

    def _invalidate_active_mode_marker(self) -> None:
        try:
            self._state_file.unlink(missing_ok=True)
        except OSError:
            # snapshot() still validates marker contents. A later launcher run
            # atomically replaces the file, so marker cleanup is best effort.
            pass

    @staticmethod
    def _terminate_process_group(process: Any) -> None:
        """Bound a stuck launcher without signaling the controller itself."""

        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and pid > 0:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            except OSError:
                pass
        else:
            try:
                process.terminate()
            except (AttributeError, OSError):
                return

        try:
            process.wait(timeout=TERMINATE_GRACE_SEC)
            return
        except subprocess.TimeoutExpired:
            pass

        if isinstance(pid, int) and pid > 0:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        else:
            try:
                process.kill()
            except (AttributeError, OSError):
                pass
        try:
            process.wait(timeout=TERMINATE_GRACE_SEC)
        except subprocess.TimeoutExpired:
            pass


class RuntimeControlRequestHandler(BaseHTTPRequestHandler):
    server: "RuntimeControlHttpServer"

    def log_message(self, _format: str, *_args: object) -> None:
        # The transient service journal remains quiet during ordinary polling.
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {"service": "taskplanner-runtime-control", "ready": True},
                # The launcher compares this loaded fingerprint with the
                # current source before deciding that an existing transient
                # service is current.
                extra={"code_fingerprint": LOADED_CODE_FINGERPRINT},
            )
            return
        if self.path == "/v1/runtime/status":
            if not self._authorized():
                return
            self._send_json(HTTPStatus.OK, self.server.controller.snapshot().to_dict())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/runtime/transition":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "application/json is required"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            content_length = -1
        if content_length < 1 or content_length > MAX_REQUEST_BYTES:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request size"})
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return
        if not isinstance(payload, dict) or set(payload) != {"mode"} or not isinstance(payload["mode"], str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request must contain only a runtime mode"})
            return
        try:
            accepted, snapshot = self.server.controller.start_transition(payload["mode"])
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "unsupported runtime mode"})
            return
        self._send_json(HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT, snapshot.to_dict())

    def _authorized(self) -> bool:
        supplied = self.headers.get(TOKEN_HEADER, "")
        if supplied and hmac.compare_digest(supplied, self.server.token):
            return True
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "runtime control authorization required"})
        return False

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if extra:
            payload = {**payload, **extra}
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class RuntimeControlHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], controller: RuntimeController, token: str) -> None:
        super().__init__(address, RuntimeControlRequestHandler)
        self.controller = controller
        self.token = token


def create_server(address: str, port: int, controller: RuntimeController, token: str) -> RuntimeControlHttpServer:
    if address != "127.0.0.1":
        raise ValueError("runtime control must bind to a loopback address")
    return RuntimeControlHttpServer((address, port), controller, token)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--launcher-log-file", required=True, type=Path)
    parser.add_argument("--bind-address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8150)
    parser.add_argument(
        "--rosbridge-router-port",
        type=int,
        default=int(os.environ.get("ROSBRIDGE_DEBUG_PORT", "9091")),
    )
    parser.add_argument(
        "--rosbridge-live-path",
        default=os.environ.get("VITE_ROSBRIDGE_LIVE_TAILSCALE_PATH", "/live"),
    )
    parser.add_argument(
        "--rosbridge-llm-path",
        default=os.environ.get("VITE_ROSBRIDGE_LLM_TAILSCALE_PATH", "/llm"),
    )
    parser.add_argument(
        "--rosbridge-replay-path",
        default=os.environ.get("VITE_ROSBRIDGE_SHADOW_TAILSCALE_PATH", "/shadow"),
    )
    parser.add_argument(
        "--transition-timeout-sec",
        type=float,
        default=float(os.environ.get("TASKPLANNER_RUNTIME_TRANSITION_TIMEOUT_SEC", DEFAULT_TRANSITION_TIMEOUT_SEC)),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        token = args.token_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        print(f"runtime control token is unavailable: {error}", file=sys.stderr)
        return 2
    if len(token) < 32:
        print("runtime control token is invalid", file=sys.stderr)
        return 2
    controller = RuntimeController(
        root=args.root,
        state_file=args.state_file,
        launcher_log_file=args.launcher_log_file,
        transition_timeout_sec=args.transition_timeout_sec,
        router_port=args.rosbridge_router_port,
        route_paths={
            "live": args.rosbridge_live_path,
            "llm-surgeon": args.rosbridge_llm_path,
            "replay": args.rosbridge_replay_path,
            "debug": "/",
        },
    )
    try:
        server = create_server(args.bind_address, args.port, controller, token)
    except (OSError, ValueError) as error:
        print(f"could not start runtime control service: {error}", file=sys.stderr)
        return 2
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
