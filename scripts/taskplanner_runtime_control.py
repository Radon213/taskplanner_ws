#!/usr/bin/env python3
"""Loopback-only, allowlisted Taskplanner runtime-mode controller.

The dashboard never receives shell access.  It can request one of the four
reviewed runtime profiles through a Vite reverse proxy that adds the local
control token.  This process in turn invokes the existing launcher with a
fixed argv and publishes only coarse, non-sensitive transition state.
"""

from __future__ import annotations

import argparse
import fcntl
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
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, asdict, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Final

import yaml


ALLOWED_MODES: Final[frozenset[str]] = frozenset({"live", "llm-surgeon", "replay", "debug"})
TOKEN_HEADER: Final[str] = "X-Taskplanner-Runtime-Control-Token"
REQUEST_ID_HEADER: Final[str] = "X-Taskplanner-Request-Id"
REQUEST_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
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
LIVE_ENDPOINT_SOURCES: Final[frozenset[str]] = frozenset({"virtual", "external"})
RUNTIME_PROFILE_MISMATCH_CODE: Final[str] = "runtime_profile_mismatch"
TASKPLANNER_RUNTIME_MODE_ENV: Final[str] = "TASKPLANNER_RUNTIME_MODE"
ASR_COMPOSE_SERVICE: Final[str] = "taskplanner-asr"
ASR_RESTART_LOCK_TIMEOUT_SEC: Final[float] = 30.0
ASR_RESTART_COMMAND_TIMEOUT_SEC: Final[float] = 75.0
ASR_RESTART_HEALTH_TIMEOUT_SEC: Final[float] = 35.0
ASR_RESTART_POLL_INTERVAL_SEC: Final[float] = 0.25
ASR_GRAPH_VERIFY_TIMEOUT_SEC: Final[float] = 18.0
ASR_GRAPH_VERIFY_POLL_INTERVAL_SEC: Final[float] = 0.5
ASR_SOURCE_MANIFEST: Final[tuple[str, ...]] = (
    "operational_asr_node.py",
    "asr_runtime.py",
    "asr_endpoints.py",
    "asr_health_monitor.py",
    "puzzle_asr_postprocess.py",
)
ASR_ABI_CONTRACT_RELATIVE_PATHS: Final[tuple[str, ...]] = (
    "src/integration_debug/package.xml",
    "src/integration_debug/setup.py",
    "src/integration_debug/setup.cfg",
    "src/surgical_msgs/package.xml",
    "src/surgical_msgs/msg/SpeechUtterance.msg",
    "src/surgical_msgs/srv/AsrControl.srv",
)
ASR_ABI_CONTRACT_MARKER: Final[str] = ".taskplanner-asr-abi-contract-v1"
ASR_IMPORT_PROBE_SHELL: Final[str] = (
    "source /opt/ros/jazzy/setup.bash; "
    "source /opt/btops_ws/install/setup.bash; "
    "source /workspaces/taskplanner_ws/install/docker/setup.bash; "
    "exec env PYTHONDONTWRITEBYTECODE=1 python3 -c \"$1\""
)
ASR_EXPECTED_IMPORT_PATH: Final[str] = (
    "/workspaces/taskplanner_ws/src/integration_debug/integration_debug/"
    "operational_asr_node.py"
)
ASR_IMPORT_PROBE_CODE: Final[str] = """\
import importlib
import importlib.util
import py_compile
import sys
from pathlib import Path

manifest = (
    "operational_asr_node.py",
    "asr_runtime.py",
    "asr_endpoints.py",
    "asr_health_monitor.py",
    "puzzle_asr_postprocess.py",
)
module_names = tuple(
    "integration_debug." + filename.removesuffix(".py") for filename in manifest
)
if any(module_name in sys.modules for module_name in module_names):
    raise SystemExit(40)
source_roots = (
    Path("/workspaces/taskplanner_ws/src/integration_debug/integration_debug"),
    Path("/workspaces/taskplanner_ws/build/docker/integration_debug/integration_debug"),
)
for source_root in source_roots:
    for filename in manifest:
        source_path = source_root / filename
        for optimization in (None, "1", "2"):
            cache_path = Path(
                importlib.util.cache_from_source(
                    str(source_path), optimization=optimization
                )
            )
            cache_path.unlink(missing_ok=True)

build_root = source_roots[1]
active_optimization = None if sys.flags.optimize == 0 else str(sys.flags.optimize)
for filename in manifest:
    source_path = build_root / filename
    cache_path = Path(
        importlib.util.cache_from_source(
            str(source_path), optimization=active_optimization
        )
    )
    py_compile.compile(
        str(source_path),
        cfile=str(cache_path),
        dfile=str(source_path),
        doraise=True,
        optimize=sys.flags.optimize,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    )

module = importlib.import_module("integration_debug.operational_asr_node")
loaded_path = Path(module.__file__).resolve()
expected_path = Path(
    "/workspaces/taskplanner_ws/src/integration_debug/integration_debug/"
    "operational_asr_node.py"
)
if loaded_path != expected_path:
    raise SystemExit(41)
for filename, module_name in zip(manifest, module_names):
    loaded_module = sys.modules.get(module_name)
    expected_cache = Path(
        importlib.util.cache_from_source(
            str(build_root / filename), optimization=active_optimization
        )
    )
    cache_header = expected_cache.read_bytes()[:8] if expected_cache.is_file() else b""
    if (
        loaded_module is None
        or Path(str(getattr(loaded_module, "__cached__", ""))) != expected_cache
        or len(cache_header) != 8
        or int.from_bytes(cache_header[4:8], "little") & 0x03 != 0x03
    ):
        raise SystemExit(42)
print(loaded_path)
"""
ASR_TOPIC_GRAPH_PROBE_SHELL: Final[str] = (
    "set -o pipefail; "
    "source /opt/ros/jazzy/setup.bash; "
    "source /opt/btops_ws/install/setup.bash; "
    "source /workspaces/taskplanner_ws/install/docker/setup.bash; "
    "timeout 7 ros2 topic info --no-daemon --spin-time 1.5 --verbose "
    "/input/asr/runtime_status | head -c 8193"
)
ASR_SERVICE_GRAPH_PROBE_SHELL: Final[str] = (
    "set -o pipefail; "
    "source /opt/ros/jazzy/setup.bash; "
    "source /opt/btops_ws/install/setup.bash; "
    "source /workspaces/taskplanner_ws/install/docker/setup.bash; "
    "timeout 7 ros2 service info --no-daemon --spin-time 1.5 "
    "/input/asr/control | head -c 8193"
)
ASR_NODE_GRAPH_PROBE_SHELL: Final[str] = (
    "set -o pipefail; "
    "source /opt/ros/jazzy/setup.bash; "
    "source /opt/btops_ws/install/setup.bash; "
    "source /workspaces/taskplanner_ws/install/docker/setup.bash; "
    "timeout 7 ros2 node info --no-daemon --spin-time 1.5 "
    "/taskplanner_asr | head -c 8193"
)
ASR_GRAPH_PROBE_MAX_BYTES: Final[int] = 8192
CORE_RUNTIME_ROSBRIDGE_MODES: Final[frozenset[str]] = frozenset(
    {"live", "llm-surgeon"}
)
CORE_RUNTIME_ROSBRIDGE_PORT_ENV: Final[str] = "ROSBRIDGE_PORT"
MODE_REQUIRED_HEALTHY_SERVICES: Final[dict[str, frozenset[str]]] = {
    "live": frozenset(
        {"ninfer-manager", "webapp", "public-rosbridge", "taskplanner-asr"}
    ),
    "llm-surgeon": frozenset({"ninfer-manager", "webapp", "public-rosbridge"}),
    "replay": frozenset({"ninfer-manager", "webapp", "public-rosbridge"}),
    # Debug intentionally treats NInfer as best-effort. Its core diagnostics
    # remain useful without a loaded model, while the web surface is required.
    "debug": frozenset({"webapp"}),
}

# `taskplanner-runtime` is shared by the Live and LLM Surgeon Compose
# profiles.  A service name and an old launcher marker therefore cannot tell
# us which contract is actually running.  Keep the small, safety-relevant
# discriminator here rather than inferring it from a UI preference.
TASKPLANNER_RUNTIME_MODE_ENV_REQUIREMENTS: Final[dict[str, dict[str, str]]] = {
    "live": {
        TASKPLANNER_RUNTIME_MODE_ENV: "live",
        "INPUT_PROFILE": "external",
        "EXECUTION_BACKEND": "action",
    },
    "llm-surgeon": {
        TASKPLANNER_RUNTIME_MODE_ENV: "llm-surgeon",
        "INPUT_PROFILE": "simulation",
        "EXECUTION_BACKEND": "mock",
    },
}


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
    diagnostic_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AsrRestartSnapshot:
    """Small polling contract for one allowlisted ASR sidecar restart."""

    phase: str
    generation: int
    job_id: str | None
    request_id: str | None
    message: str
    retryable: bool
    source_revision: str | None
    container_started_at: str | None
    before_pid: int | None
    after_pid: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AsrContainerState:
    status: str
    running: bool
    restarting: bool
    pid: int
    started_at: str
    health: str


@dataclass(frozen=True)
class AsrContainerIdentity:
    image_id: str
    user: str


class AsrRestartError(RuntimeError):
    """Internal error carrying only a reviewed, non-sensitive UI message."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.public_message = message
        self.retryable = retryable


def runtime_install_contract_fingerprint(root: Path) -> str:
    """Mirror the launcher's ABI/install fingerprint without invoking a shell."""

    source_root = root.resolve() / "src"
    names = {"CMakeLists.txt", "package.xml", "setup.py", "setup.cfg"}
    suffixes = {".action", ".c", ".cc", ".cpp", ".h", ".hpp", ".idl", ".msg", ".srv"}
    ignored = {".pytest_cache", "__pycache__", "build", "install", "log"}
    digest = hashlib.sha256()
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or any(part in ignored for part in path.parts):
            continue
        if path.name not in names and path.suffix not in suffixes:
            continue
        digest.update(path.relative_to(root.resolve()).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def runtime_install_contract_matches_source(root: Path) -> bool:
    """Fail closed when a Python-only restart cannot apply changed ABI files."""

    resolved_root = root.resolve()
    marker = resolved_root / "install" / "docker" / ".taskplanner-runtime-abi-contract-v1"
    setup = resolved_root / "install" / "docker" / "setup.bash"
    entrypoint = (
        resolved_root
        / "install"
        / "docker"
        / "integration_debug"
        / "lib"
        / "integration_debug"
        / "operational_asr_node"
    )
    try:
        recorded = marker.read_text(encoding="utf-8").strip()
        expected = runtime_install_contract_fingerprint(resolved_root)
    except OSError:
        return False
    return (
        bool(re.fullmatch(r"[0-9a-f]{64}", recorded))
        and recorded == expected
        and setup.is_file()
        and entrypoint.is_file()
        and os.access(entrypoint, os.X_OK)
    )


def asr_install_contract_fingerprint(root: Path) -> str:
    """Fingerprint only packaging and generated interfaces imported by ASR."""

    resolved_root = root.resolve()
    digest = hashlib.sha256()
    for relative in ASR_ABI_CONTRACT_RELATIVE_PATHS:
        path = resolved_root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def asr_install_contract_matches_source(root: Path) -> bool:
    """Check the dedicated ASR install marker and required entry point."""

    resolved_root = root.resolve()
    marker = resolved_root / "install" / "docker" / ASR_ABI_CONTRACT_MARKER
    setup = resolved_root / "install" / "docker" / "setup.bash"
    entrypoint = (
        resolved_root
        / "install"
        / "docker"
        / "integration_debug"
        / "lib"
        / "integration_debug"
        / "operational_asr_node"
    )
    try:
        recorded = marker.read_text(encoding="utf-8").strip()
        expected = asr_install_contract_fingerprint(resolved_root)
    except OSError:
        return False
    return (
        bool(re.fullmatch(r"[0-9a-f]{64}", recorded))
        and recorded == expected
        and setup.is_file()
        and entrypoint.is_file()
        and os.access(entrypoint, os.X_OK)
    )


def ensure_asr_install_contract(root: Path) -> bool:
    """Bootstrap/migrate the scoped marker only from a trusted global marker."""

    resolved_root = root.resolve()
    marker = resolved_root / "install" / "docker" / ASR_ABI_CONTRACT_MARKER
    if marker.exists() and asr_install_contract_matches_source(resolved_root):
        return True
    if not runtime_install_contract_matches_source(resolved_root):
        return False
    temporary: Path | None = None
    try:
        expected = asr_install_contract_fingerprint(resolved_root)
        temporary = marker.with_name(f"{marker.name}.tmp.{os.getpid()}")
        temporary.write_text(f"{expected}\n", encoding="utf-8")
        os.replace(temporary, marker)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return False
    return asr_install_contract_matches_source(resolved_root)


def asr_source_revision(root: Path) -> str:
    """Exactly mirror the source revision published by OperationalAsrNode."""

    package_dir = (
        root.resolve() / "src" / "integration_debug" / "integration_debug"
    )
    digest = hashlib.sha256()
    for filename in ASR_SOURCE_MANIFEST:
        encoded_name = filename.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(2, "big"))
        digest.update(encoded_name)
        try:
            source = (package_dir / filename).read_bytes()
        except FileNotFoundError:
            digest.update(b"\x00missing")
            continue
        except OSError:
            digest.update(b"\x00unreadable")
            continue
        digest.update(b"\x00present")
        digest.update(len(source).to_bytes(8, "big"))
        digest.update(source)
    return digest.hexdigest()


@contextmanager
def launcher_lock(lock_file: Path, timeout_sec: float):
    """Acquire the launcher's exact advisory lock with a bounded wait."""

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout_sec
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AsrRestartError(
                        "Another Taskplanner start or stop operation is still in progress."
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _run_fixed_command(
    runner: Callable[..., Any],
    command: list[str],
    *,
    timeout: float,
) -> Any:
    try:
        result = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AsrRestartError("The ASR container command did not complete.") from error
    if result.returncode != 0:
        raise AsrRestartError("The ASR container command failed.")
    return result


def resolve_asr_container(root: Path, runner: Callable[..., Any]) -> str:
    """Resolve one existing ASR container, including a stopped/crash-loop one."""

    result = _run_fixed_command(
        runner,
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project.working_dir={root.resolve()}",
            "--filter",
            f"label=com.docker.compose.service={ASR_COMPOSE_SERVICE}",
            "--format",
            "{{.ID}}",
        ],
        timeout=2.0,
    )
    identifiers = result.stdout.split()
    if len(identifiers) != 1 or re.fullmatch(r"[0-9a-f]{12,64}", identifiers[0]) is None:
        raise AsrRestartError(
            "Exactly one existing Taskplanner ASR container is required."
        )
    return identifiers[0]


def inspect_asr_container(
    root: Path,
    container_id: str,
    runner: Callable[..., Any],
) -> AsrContainerState:
    """Read only the target identity labels and bounded Docker state."""

    if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
        raise AsrRestartError("The resolved ASR container identity is invalid.")
    labels_result = _run_fixed_command(
        runner,
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", container_id],
        timeout=2.0,
    )
    state_result = _run_fixed_command(
        runner,
        ["docker", "inspect", "--format", "{{json .State}}", container_id],
        timeout=2.0,
    )
    try:
        labels = json.loads(labels_result.stdout)
        state = json.loads(state_result.stdout)
    except (TypeError, ValueError) as error:
        raise AsrRestartError("The ASR container state could not be verified.") from error
    if not isinstance(labels, dict) or (
        labels.get("com.docker.compose.project.working_dir") != str(root.resolve())
        or labels.get("com.docker.compose.service") != ASR_COMPOSE_SERVICE
    ):
        raise AsrRestartError("The ASR container identity changed unexpectedly.")
    if not isinstance(state, dict):
        raise AsrRestartError("The ASR container state could not be verified.")
    status = state.get("Status")
    running = state.get("Running")
    restarting = state.get("Restarting")
    pid = state.get("Pid")
    started_at = state.get("StartedAt")
    health = state.get("Health")
    health_status = health.get("Status") if isinstance(health, dict) else None
    if health_status is None and (running is False or restarting is True):
        health_status = "unavailable"
    if (
        not isinstance(status, str)
        or status
        not in {
            "created",
            "running",
            "paused",
            "restarting",
            "removing",
            "exited",
            "dead",
        }
        or not isinstance(running, bool)
        or not isinstance(restarting, bool)
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or not isinstance(started_at, str)
        or not 1 <= len(started_at) <= 128
        or not isinstance(health_status, str)
        or len(health_status) > 32
    ):
        raise AsrRestartError("The ASR container state could not be verified.")
    return AsrContainerState(
        status=status,
        running=running,
        restarting=restarting,
        pid=pid,
        started_at=started_at,
        health=health_status,
    )


def inspect_asr_container_identity(
    root: Path,
    container_id: str,
    runner: Callable[..., Any],
) -> AsrContainerIdentity:
    """Verify immutable image and numeric non-root user of the exact target."""

    if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
        raise AsrRestartError("The resolved ASR container identity is invalid.")
    image_result = _run_fixed_command(
        runner,
        ["docker", "inspect", "--format", "{{json .Image}}", container_id],
        timeout=2.0,
    )
    user_result = _run_fixed_command(
        runner,
        ["docker", "inspect", "--format", "{{json .Config.User}}", container_id],
        timeout=2.0,
    )
    workdir_result = _run_fixed_command(
        runner,
        [
            "docker",
            "inspect",
            "--format",
            "{{json .Config.WorkingDir}}",
            container_id,
        ],
        timeout=2.0,
    )
    mounts_result = _run_fixed_command(
        runner,
        ["docker", "inspect", "--format", "{{json .Mounts}}", container_id],
        timeout=2.0,
    )
    try:
        image_id = json.loads(image_result.stdout)
        user = json.loads(user_result.stdout)
        working_dir = json.loads(workdir_result.stdout)
        mounts = json.loads(mounts_result.stdout)
    except (TypeError, ValueError) as error:
        raise AsrRestartError("The ASR container identity could not be verified.") from error
    if (
        not isinstance(image_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
        or not isinstance(user, str)
        or re.fullmatch(r"[1-9][0-9]{0,9}:[1-9][0-9]{0,9}", user) is None
        or working_dir != "/workspaces/taskplanner_ws"
        or not isinstance(mounts, list)
    ):
        raise AsrRestartError("The ASR container identity could not be verified.")
    workspace_mounts = [
        mount
        for mount in mounts
        if isinstance(mount, dict)
        and mount.get("Destination") == "/workspaces/taskplanner_ws"
    ]
    if len(workspace_mounts) != 1 or (
        workspace_mounts[0].get("Type") != "bind"
        or workspace_mounts[0].get("Source") != str(root.resolve())
        or workspace_mounts[0].get("RW") is not True
    ):
        raise AsrRestartError("The ASR workspace bind could not be verified.")
    # Recheck the Compose labels after reading the immutable image/user fields.
    labels_result = _run_fixed_command(
        runner,
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", container_id],
        timeout=2.0,
    )
    try:
        labels = json.loads(labels_result.stdout)
    except (TypeError, ValueError) as error:
        raise AsrRestartError("The ASR container identity could not be verified.") from error
    if not isinstance(labels, dict) or (
        labels.get("com.docker.compose.project.working_dir") != str(root.resolve())
        or labels.get("com.docker.compose.service") != ASR_COMPOSE_SERVICE
    ):
        raise AsrRestartError("The ASR container identity changed unexpectedly.")
    return AsrContainerIdentity(image_id=image_id, user=user)


def preflight_asr_import(container_id: str, runner: Callable[..., Any]) -> None:
    """Purge, checked-hash compile, and import the fixed ASR source manifest."""

    result = _run_fixed_command(
        runner,
        [
            "docker",
            "exec",
            container_id,
            "bash",
            "-lc",
            ASR_IMPORT_PROBE_SHELL,
            "--",
            ASR_IMPORT_PROBE_CODE,
        ],
        timeout=12.0,
    )
    _validate_asr_preflight_output(result.stdout)


def _validate_asr_preflight_output(output: str) -> None:
    output_lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not output_lines or output_lines[-1] != ASR_EXPECTED_IMPORT_PATH:
        raise AsrRestartError(
            "The edited ASR source did not import from the reviewed workspace path."
        )


def preflight_asr_import_isolated(
    root: Path,
    identity: AsrContainerIdentity,
    runner: Callable[..., Any],
) -> None:
    """Preflight a stopped ASR using its image without network or extra mounts.

    The workspace bind is intentionally writable only so the fixed manifest's
    checked-hash cache files can replace stale timestamp pyc before the exact
    existing container starts. The ephemeral root is read-only, capabilities
    are dropped, and no run/recording/audio mounts are exposed.
    """

    result = _run_fixed_command(
        runner,
        [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--pids-limit",
            "128",
            "--memory",
            "512m",
            "--user",
            identity.user,
            "--workdir",
            "/workspaces/taskplanner_ws",
            "--volume",
            f"{root.resolve()}:/workspaces/taskplanner_ws:rw",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--entrypoint",
            "bash",
            identity.image_id,
            "-lc",
            ASR_IMPORT_PROBE_SHELL,
            "--",
            ASR_IMPORT_PROBE_CODE,
        ],
        timeout=30.0,
    )
    _validate_asr_preflight_output(result.stdout)


def verify_unique_asr_status_publisher(
    container_id: str,
    runner: Callable[..., Any],
    *,
    deadline: float | None = None,
) -> None:
    """Require one reviewed ASR node owning the status and control endpoints."""

    absolute_deadline = (
        time.monotonic() + ASR_GRAPH_VERIFY_TIMEOUT_SEC
        if deadline is None
        else deadline
    )

    def command_timeout() -> float:
        remaining = absolute_deadline - time.monotonic()
        if remaining <= 0:
            raise AsrRestartError("The ASR ROS graph probe timed out.")
        return min(9.0, remaining)

    topic_result = _run_fixed_command(
        runner,
        [
            "docker",
            "exec",
            container_id,
            "bash",
            "-lc",
            ASR_TOPIC_GRAPH_PROBE_SHELL,
        ],
        timeout=command_timeout(),
    )
    service_result = _run_fixed_command(
        runner,
        [
            "docker",
            "exec",
            container_id,
            "bash",
            "-lc",
            ASR_SERVICE_GRAPH_PROBE_SHELL,
        ],
        timeout=command_timeout(),
    )
    node_result = _run_fixed_command(
        runner,
        [
            "docker",
            "exec",
            container_id,
            "bash",
            "-lc",
            ASR_NODE_GRAPH_PROBE_SHELL,
        ],
        timeout=command_timeout(),
    )
    topic_output = topic_result.stdout
    service_output = service_result.stdout
    node_output = node_result.stdout
    if (
        len(topic_output.encode("utf-8", errors="replace"))
        > ASR_GRAPH_PROBE_MAX_BYTES
        or len(service_output.encode("utf-8", errors="replace"))
        > ASR_GRAPH_PROBE_MAX_BYTES
        or len(node_output.encode("utf-8", errors="replace"))
        > ASR_GRAPH_PROBE_MAX_BYTES
    ):
        raise AsrRestartError("The ASR ROS graph response exceeded its limit.")
    publisher_counts = re.findall(
        r"^Publisher count:\s*(\d+)\s*$", topic_output, flags=re.MULTILINE
    )
    topic_types = re.findall(
        r"^Type:\s*(\S+)\s*$", topic_output, flags=re.MULTILINE
    )
    publisher_section = topic_output.split("Subscription count:", 1)[0]
    node_names = re.findall(
        r"^Node name:\s*(\S+)\s*$", publisher_section, flags=re.MULTILINE
    )
    node_namespaces = re.findall(
        r"^Node namespace:\s*(\S+)\s*$", publisher_section, flags=re.MULTILINE
    )
    endpoint_types = re.findall(
        r"^Endpoint type:\s*(\S+)\s*$", publisher_section, flags=re.MULTILINE
    )
    gids = re.findall(
        r"^GID:\s*([0-9A-Fa-f]{2}(?:[.:][0-9A-Fa-f]{2}){15})\s*$",
        publisher_section,
        flags=re.MULTILINE,
    )
    service_types = re.findall(
        r"^Type:\s*(\S+)\s*$", service_output, flags=re.MULTILINE
    )
    service_counts = re.findall(
        r"^Services count:\s*(\d+)\s*$", service_output, flags=re.MULTILINE
    )
    node_service_section = node_output.split("Service Servers:", 1)
    owned_services: list[tuple[str, str]] = []
    if len(node_service_section) == 2:
        service_server_body = node_service_section[1].split("Service Clients:", 1)[0]
        owned_services = re.findall(
            r"^\s{4}(\S+):\s+(\S+)\s*$",
            service_server_body,
            flags=re.MULTILINE,
        )
    if (
        publisher_counts != ["1"]
        or topic_types != ["std_msgs/msg/String"]
        or node_names != ["taskplanner_asr"]
        or node_namespaces != ["/"]
        or endpoint_types != ["PUBLISHER"]
        or len(gids) != 1
        or len(set(gids)) != 1
        or service_types != ["surgical_msgs/srv/AsrControl"]
        or service_counts != ["1"]
        or node_output.splitlines()[:1] != ["/taskplanner_asr"]
        or owned_services.count(
            ("/input/asr/control", "surgical_msgs/srv/AsrControl")
        )
        != 1
    ):
        raise AsrRestartError(
            "The ASR ROS graph does not contain exactly one reviewed node endpoint."
        )


def runtime_request_is_already_ready(
    *,
    requested_mode: str,
    active_mode: str | None,
    running: bool | None,
    contract_matches: bool | None,
    running_modes: set[str] | None,
    required_plane_ready: bool | None,
) -> bool:
    """Return true only for an unambiguous, healthy same-mode request.

    A repeated dashboard selection is not a runtime transition.  It must not
    require the procedure to stop or invoke the launcher, but it may be
    treated as a no-op only while the marker, the service probe, the container
    contract, and independent running-mode discovery all agree.
    """

    return (
        requested_mode in ALLOWED_MODES
        and active_mode == requested_mode
        and running is True
        and contract_matches is True
        and running_modes == {requested_mode}
        and required_plane_ready is True
    )


def read_active_mode(state_file: Path) -> str | None:
    """Read only the launcher-written, allowlisted mode marker."""

    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    mode = payload.get("mode") if isinstance(payload, dict) else None
    return mode if mode in ALLOWED_MODES else None


def _container_environment(container_id: str) -> dict[str, str] | None:
    """Read the running container's non-secret mode contract without logging it."""

    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{range .Config.Env}}{{println .}}{{end}}",
                container_id,
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
    environment: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            environment[key] = value
    return environment


def taskplanner_runtime_mode_from_environment(
    environment: dict[str, str] | None,
) -> str | None:
    """Return a verified core mode, or ``None`` for an incomplete contract."""

    if environment is None:
        return None
    for mode, requirements in TASKPLANNER_RUNTIME_MODE_ENV_REQUIREMENTS.items():
        if all(environment.get(key, "") == expected for key, expected in requirements.items()):
            return mode
    return None


def classify_taskplanner_runtime_environment(
    environment: dict[str, str] | None,
) -> str | None:
    """Classify a running core for safe replacement, even if its marker is stale.

    This is deliberately less strict than
    :func:`taskplanner_runtime_mode_from_environment`: a legacy/mock container
    without the new marker must still be identified as LLM Surgeon so the
    controller can require its authoritative stopped-state check and replace it
    through the reviewed launcher.  It is never accepted as a valid *recorded*
    Live/LLM mode without the complete marker contract.
    """

    if environment is None:
        return None
    input_profile = environment.get("INPUT_PROFILE", "").strip().lower()
    execution_backend = environment.get("EXECUTION_BACKEND", "").strip().lower()
    if input_profile == "external":
        return "live"
    if input_profile == "simulation" and execution_backend == "mock":
        return "llm-surgeon"
    return None


def inspect_taskplanner_runtime_mode(root: Path) -> str | None:
    """Identify the active taskplanner-runtime from its container contract."""

    try:
        container_id = _running_mode_container_id(root, "live")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if container_id is None:
        return None
    return classify_taskplanner_runtime_environment(_container_environment(container_id))


def mode_runtime_contract_matches(root: Path, mode: str) -> bool | None:
    """Check whether the shared runtime container matches an active mode marker."""

    if mode not in {"live", "llm-surgeon"}:
        return True
    try:
        container_id = _running_mode_container_id(root, mode)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if container_id is None:
        return None
    actual_mode = taskplanner_runtime_mode_from_environment(
        _container_environment(container_id)
    )
    if actual_mode is None:
        # A container is present but its contract is incomplete or malformed.
        # Treat that as a mismatch, not a transient ROS/bridge outage.
        return False
    return actual_mode == mode


def core_runtime_rosbridge_ready(root: Path, mode: str) -> bool | None:
    """Probe the core runtime bridge, never an optional LAN/Tailnet router.

    Live and LLM Surgeon share ``taskplanner-runtime`` and bind their browser
    bridge directly on the loopback port declared by that container.  The
    9091 path router is an optional Ops-plane component, so treating it as a
    core liveness requirement would incorrectly erase a healthy Live marker.
    """

    if mode not in CORE_RUNTIME_ROSBRIDGE_MODES:
        return None
    try:
        container_id = _running_mode_container_id(root, mode)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if container_id is None:
        return None
    environment = _container_environment(container_id)
    if environment is None:
        return None
    raw_port = environment.get(CORE_RUNTIME_ROSBRIDGE_PORT_ENV, "").strip()
    try:
        port = int(raw_port)
    except ValueError:
        return False
    if not 1 <= port <= 65535:
        return False
    return websocket_route_ready(mode, port=port, route_paths={mode: "/"})


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
    contract_matches = mode_runtime_contract_matches(root, mode)
    if contract_matches is not True:
        return contract_matches
    if mode in CORE_RUNTIME_ROSBRIDGE_MODES:
        return core_runtime_rosbridge_ready(root, mode)
    return websocket_route_ready(mode, port=router_port, route_paths=route_paths)


def mode_required_plane_ready(root: Path, mode: str) -> bool | None:
    """Check mandatory same-mode sidecars with one bounded Docker query.

    Core process and ROSBridge route health are checked independently by
    :func:`compose_service_running`. Services listed here all declare Compose
    healthchecks, so a merely running or still-starting container cannot make a
    repeated mode request look like a healthy no-op.
    """

    required_services = MODE_REQUIRED_HEALTHY_SERVICES.get(mode)
    if required_services is None:
        return False
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
                (
                    '{{.Label "com.docker.compose.service"}}'
                    "\t{{.State}}\t{{.Status}}"
                ),
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
    services: dict[str, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0]:
            services[parts[0]] = (parts[1], parts[2])
    return all(
        services.get(service, ("", ""))[0] == "running"
        and "(healthy)" in services.get(service, ("", ""))[1]
        for service in required_services
    )


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
        # Live and LLM share this service.  Never guess Live from its service
        # name: an accidental Compose recreate otherwise leaves the dashboard
        # in Live while a mock runtime is actually running.
        mode = inspect_taskplanner_runtime_mode(root)
        if mode is None:
            return None
        candidates.add(mode)
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

    # Live and LLM Surgeon share a Compose service, but candidate discovery
    # reads its reviewed environment contract and therefore distinguishes the
    # two modes. A warm restart must reserve exactly the recorded mode.
    expected_candidate = expected_mode
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
                "source /workspaces/taskplanner_ws/install/docker/setup.bash; "
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
                "source /workspaces/taskplanner_ws/install/docker/setup.bash; "
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
        mode_contract_probe: Callable[[str], bool | None] | None = None,
        required_plane_probe: Callable[[str], bool | None] | None = None,
        router_port: int = 9091,
        route_paths: dict[str, str] | None = None,
        running_modes_probe: Callable[[], set[str] | None] | None = None,
        command_runner: Callable[..., Any] | None = None,
        asr_install_contract_probe: Callable[[], bool] | None = None,
        asr_source_revision_probe: Callable[[], str] | None = None,
        asr_restart_lock_timeout_sec: float = ASR_RESTART_LOCK_TIMEOUT_SEC,
        asr_restart_command_timeout_sec: float = ASR_RESTART_COMMAND_TIMEOUT_SEC,
        asr_restart_health_timeout_sec: float = ASR_RESTART_HEALTH_TIMEOUT_SEC,
        asr_restart_poll_interval_sec: float = ASR_RESTART_POLL_INTERVAL_SEC,
        asr_graph_verify_timeout_sec: float = ASR_GRAPH_VERIFY_TIMEOUT_SEC,
        asr_graph_verify_poll_interval_sec: float = (
            ASR_GRAPH_VERIFY_POLL_INTERVAL_SEC
        ),
    ) -> None:
        if transition_timeout_sec <= 0:
            raise ValueError("transition timeout must be positive")
        if active_probe_ttl_sec < 0:
            raise ValueError("active probe TTL must not be negative")
        if active_probe_failure_threshold < 1:
            raise ValueError("active probe failure threshold must be positive")
        if (
            asr_restart_lock_timeout_sec <= 0
            or asr_restart_command_timeout_sec <= 0
            or asr_restart_health_timeout_sec <= 0
            or asr_restart_poll_interval_sec <= 0
            or asr_graph_verify_timeout_sec <= 0
            or asr_graph_verify_poll_interval_sec <= 0
        ):
            raise ValueError("ASR restart timeouts must be positive")
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
        self._mode_contract_probe = mode_contract_probe or (
            lambda mode: mode_runtime_contract_matches(self._root, mode)
        )
        self._required_plane_probe = required_plane_probe or (
            lambda mode: mode_required_plane_ready(self._root, mode)
        )
        self._running_modes_probe = running_modes_probe or (
            lambda: detect_running_mode_candidates(self._root)
        )
        self._command_runner = command_runner or subprocess.run
        self._asr_install_contract_probe = asr_install_contract_probe or (
            lambda: ensure_asr_install_contract(self._root)
        )
        self._asr_source_revision_probe = asr_source_revision_probe or (
            lambda: asr_source_revision(self._root)
        )
        self._asr_restart_lock_timeout_sec = float(asr_restart_lock_timeout_sec)
        self._asr_restart_command_timeout_sec = float(
            asr_restart_command_timeout_sec
        )
        self._asr_restart_health_timeout_sec = float(asr_restart_health_timeout_sec)
        self._asr_restart_poll_interval_sec = float(
            asr_restart_poll_interval_sec
        )
        self._asr_graph_verify_timeout_sec = float(asr_graph_verify_timeout_sec)
        self._asr_graph_verify_poll_interval_sec = float(
            asr_graph_verify_poll_interval_sec
        )
        self._last_probe_at = 0.0
        self._last_probe_mode: str | None = None
        self._last_probe_running: bool | None = None
        self._consecutive_probe_failures = 0
        self._lock = threading.RLock()
        self._phase = "idle"
        self._requested_mode: str | None = None
        self._message = "Runtime mode control is ready."
        self._diagnostic_code: str | None = None
        self._process: Any | None = None
        self._output: Any | None = None
        self._asr_generation = 0
        self._asr_job_active = False
        self._asr_snapshot = AsrRestartSnapshot(
            phase="idle",
            generation=0,
            job_id=None,
            request_id=None,
            message="ASR node restart control is ready.",
            retryable=False,
            source_revision=None,
            container_started_at=None,
            before_pid=None,
            after_pid=None,
        )

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
                diagnostic_code=self._diagnostic_code,
            )

    def asr_restart_snapshot(self) -> AsrRestartSnapshot:
        with self._lock:
            return self._asr_snapshot

    def _authoritative_live_mode_locked(self) -> bool:
        """Require marker, core contract, and discovery to agree on Live."""

        if self._phase != "idle" or self._reconcile_active_mode_locked() != "live":
            return False
        try:
            running = self._mode_running_probe("live")
            contract_matches = self._mode_contract_probe("live")
            running_modes = self._running_modes_probe()
        except Exception:
            return False
        return (
            running is True
            and contract_matches is True
            and running_modes == {"live"}
        )

    def start_asr_restart(
        self, request_id: str | None = None
    ) -> tuple[bool, AsrRestartSnapshot]:
        """Queue one ASR-only restart without blocking the HTTP request."""

        normalized_request_id = request_id or str(uuid.uuid4())
        with self._lock:
            if self._asr_job_active:
                return False, self._asr_snapshot
            if not self._authoritative_live_mode_locked():
                return False, AsrRestartSnapshot(
                    phase="failed",
                    generation=self._asr_generation,
                    job_id=None,
                    request_id=normalized_request_id,
                    message=(
                        "ASR can be restarted only while the authoritative Live "
                        "runtime is ready."
                    ),
                    retryable=True,
                    source_revision=None,
                    container_started_at=None,
                    before_pid=None,
                    after_pid=None,
                )
            self._asr_generation += 1
            generation = self._asr_generation
            job_id = uuid.uuid4().hex
            self._asr_job_active = True
            self._asr_snapshot = AsrRestartSnapshot(
                phase="queued",
                generation=generation,
                job_id=job_id,
                request_id=normalized_request_id,
                message="ASR node restart is queued.",
                retryable=False,
                source_revision=None,
                container_started_at=None,
                before_pid=None,
                after_pid=None,
            )
            snapshot = self._asr_snapshot
            threading.Thread(
                target=self._run_asr_restart,
                args=(generation, job_id),
                daemon=True,
                name=f"taskplanner-asr-restart-{generation}",
            ).start()
            return True, snapshot

    def _update_asr_restart(
        self,
        generation: int,
        job_id: str,
        **changes: Any,
    ) -> bool:
        with self._lock:
            if (
                self._asr_snapshot.generation != generation
                or self._asr_snapshot.job_id != job_id
            ):
                return False
            self._asr_snapshot = replace(self._asr_snapshot, **changes)
            return True

    def _run_asr_restart(self, generation: int, job_id: str) -> None:
        try:
            self._update_asr_restart(
                generation,
                job_id,
                phase="preflighting",
                message="Checking the edited ASR source before restart.",
            )
            with launcher_lock(
                self._state_file.parent / "launcher.lock",
                self._asr_restart_lock_timeout_sec,
            ):
                with self._lock:
                    if not self._authoritative_live_mode_locked():
                        raise AsrRestartError(
                            "The authoritative Live runtime changed before ASR restart."
                        )
                try:
                    install_contract_matches = self._asr_install_contract_probe()
                except Exception:
                    install_contract_matches = False
                if install_contract_matches is not True:
                    raise AsrRestartError(
                        "The ASR install contract changed. Run the scoped build "
                        "before restarting ASR.",
                        retryable=False,
                    )
                try:
                    source_revision = self._asr_source_revision_probe()
                except Exception as error:
                    raise AsrRestartError(
                        "The edited ASR source could not be fingerprinted."
                    ) from error
                if re.fullmatch(r"[0-9a-f]{64}", source_revision) is None:
                    raise AsrRestartError(
                        "The edited ASR source fingerprint is invalid."
                    )
                container_id = resolve_asr_container(
                    self._root, self._command_runner
                )
                identity = inspect_asr_container_identity(
                    self._root, container_id, self._command_runner
                )
                before = inspect_asr_container(
                    self._root, container_id, self._command_runner
                )
                stable_running_container = (
                    before.status == "running"
                    and before.running
                    and not before.restarting
                    and before.pid > 0
                    and before.health == "healthy"
                )
                if stable_running_container:
                    try:
                        preflight_asr_import(container_id, self._command_runner)
                    except AsrRestartError:
                        refreshed_before = inspect_asr_container(
                            self._root, container_id, self._command_runner
                        )
                        if (
                            refreshed_before.status == "running"
                            and refreshed_before.running
                            and not refreshed_before.restarting
                            and refreshed_before.pid > 0
                            and refreshed_before.health == "healthy"
                        ):
                            raise
                        before = refreshed_before
                        preflight_asr_import_isolated(
                            self._root, identity, self._command_runner
                        )
                else:
                    preflight_asr_import_isolated(
                        self._root, identity, self._command_runner
                    )
                try:
                    stable_revision = self._asr_source_revision_probe()
                except Exception as error:
                    raise AsrRestartError(
                        "The edited ASR source could not be fingerprinted."
                    ) from error
                if stable_revision != source_revision:
                    raise AsrRestartError(
                        "The ASR source changed during preflight. Wait for the "
                        "edit to finish and retry."
                    )
                self._update_asr_restart(
                    generation,
                    job_id,
                    phase="restarting",
                    message="Restarting only the ASR node container.",
                    source_revision=source_revision,
                    before_pid=before.pid,
                )
                _run_fixed_command(
                    self._command_runner,
                    ["docker", "restart", "--time", "45", container_id],
                    timeout=self._asr_restart_command_timeout_sec,
                )
                self._update_asr_restart(
                    generation,
                    job_id,
                    phase="verifying",
                    message="Waiting for the new ASR node to become healthy.",
                )
                deadline = time.monotonic() + self._asr_restart_health_timeout_sec
                after: AsrContainerState | None = None
                while time.monotonic() < deadline:
                    candidate = inspect_asr_container(
                        self._root, container_id, self._command_runner
                    )
                    if (
                        candidate.running
                        and candidate.pid > 0
                        and candidate.pid != before.pid
                        and candidate.started_at != before.started_at
                        and candidate.health == "healthy"
                    ):
                        after = candidate
                        break
                    time.sleep(self._asr_restart_poll_interval_sec)
                if after is None:
                    raise AsrRestartError(
                        "The restarted ASR node did not become healthy in time."
                    )
                if resolve_asr_container(self._root, self._command_runner) != container_id:
                    raise AsrRestartError(
                        "The ASR container identity changed during restart."
                    )
                if (
                    inspect_asr_container_identity(
                        self._root, container_id, self._command_runner
                    )
                    != identity
                ):
                    raise AsrRestartError(
                        "The ASR container image or user changed during restart."
                    )
                self._update_asr_restart(
                    generation,
                    job_id,
                    message="Verifying the restarted ASR ROS graph.",
                )
                graph_deadline = (
                    time.monotonic() + self._asr_graph_verify_timeout_sec
                )
                while True:
                    remaining_graph_time = graph_deadline - time.monotonic()
                    if remaining_graph_time <= 0:
                        raise AsrRestartError(
                            "The restarted ASR node did not become unique "
                            "on the ROS graph in time."
                        )
                    try:
                        verify_unique_asr_status_publisher(
                            container_id,
                            self._command_runner,
                            deadline=graph_deadline,
                        )
                        break
                    except AsrRestartError:
                        remaining_graph_time = graph_deadline - time.monotonic()
                        if remaining_graph_time <= 0:
                            raise AsrRestartError(
                                "The restarted ASR node did not become unique "
                                "on the ROS graph in time."
                            )
                        time.sleep(
                            min(
                                self._asr_graph_verify_poll_interval_sec,
                                remaining_graph_time,
                            )
                        )
                try:
                    final_revision = self._asr_source_revision_probe()
                except Exception as error:
                    raise AsrRestartError(
                        "The edited ASR source could not be fingerprinted after restart."
                    ) from error
                if final_revision != source_revision:
                    raise AsrRestartError(
                        "The ASR source changed during restart. Retry once editing is complete."
                    )
                self._update_asr_restart(
                    generation,
                    job_id,
                    phase="succeeded",
                    message="The ASR node restarted with the edited source.",
                    retryable=False,
                    container_started_at=after.started_at,
                    after_pid=after.pid,
                )
        except AsrRestartError as error:
            self._update_asr_restart(
                generation,
                job_id,
                phase="failed",
                message=error.public_message,
                retryable=error.retryable,
            )
        except Exception:
            self._update_asr_restart(
                generation,
                job_id,
                phase="failed",
                message="The ASR node restart failed unexpectedly.",
                retryable=True,
            )
        finally:
            with self._lock:
                if (
                    self._asr_snapshot.generation == generation
                    and self._asr_snapshot.job_id == job_id
                ):
                    self._asr_job_active = False

    def _reconcile_active_mode_locked(self) -> str | None:
        active_mode = read_active_mode(self._state_file)
        if active_mode is None:
            # A process restart must not erase a detected configuration error.
            # If a core is still running without the launcher's atomic marker,
            # surface it as an explicit recovery state instead of letting the
            # browser call the selected mode "ready" or wait forever for a
            # preflight publisher that profile never started.
            if self._phase == "idle":
                try:
                    candidates = self._running_modes_probe()
                except Exception:
                    candidates = None
                if candidates is not None and len(candidates) == 1:
                    detected_mode = next(iter(candidates))
                    self._phase = "failed"
                    self._requested_mode = detected_mode
                    self._message = (
                        "A running runtime has no reviewed mode marker. "
                        "Restart the selected mode through Taskplanner."
                    )
                    self._diagnostic_code = RUNTIME_PROFILE_MISMATCH_CODE
            return active_mode
        if self._phase == "idle" and active_mode in {"live", "llm-surgeon"}:
            try:
                contract_matches = self._mode_contract_probe(active_mode)
            except Exception:
                contract_matches = None
            if contract_matches is False:
                self._invalidate_active_mode_marker()
                self._phase = "failed"
                self._requested_mode = active_mode
                self._message = (
                    "Recorded runtime mode does not match the running container "
                    "profile. Restart the selected mode through Taskplanner."
                )
                self._diagnostic_code = RUNTIME_PROFILE_MISMATCH_CODE
                self._last_probe_at = 0.0
                self._last_probe_mode = None
                self._last_probe_running = None
                self._consecutive_probe_failures = 0
                return None
        # A reviewed direct launcher invocation can repair a runtime after an
        # earlier controller-side transition failed.  Its marker is only
        # written after the launcher has completed semantic readiness.  Do
        # not leave the browser falsely offline when that ready marker matches
        # the failed request and the core service is actually running.
        if self._phase == "failed" and self._requested_mode == active_mode:
            try:
                recovered_running = self._mode_running_probe(active_mode)
            except Exception:
                recovered_running = None
            if recovered_running is True:
                self._phase = "idle"
                self._requested_mode = None
                self._message = "Detected a ready runtime launched outside the controller."
                self._diagnostic_code = None
                self._last_probe_at = time.monotonic()
                self._last_probe_mode = active_mode
                self._last_probe_running = True
                self._consecutive_probe_failures = 0
        if self._phase != "idle":
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
            self._diagnostic_code = None
            return None
        return active_mode

    def start_transition(self, mode: str) -> tuple[bool, TransitionSnapshot]:
        if mode not in ALLOWED_MODES:
            raise ValueError("unsupported runtime mode")

        with self._lock:
            if self._asr_job_active:
                return False, TransitionSnapshot(
                    phase=self._phase,
                    active_mode=read_active_mode(self._state_file),
                    requested_mode=self._requested_mode,
                    message=(
                        "Wait for the ASR node restart to finish before changing "
                        "runtime mode."
                    ),
                    retryable=True,
                    diagnostic_code=self._diagnostic_code,
                )
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
            if active_mode == mode:
                try:
                    running = self._mode_running_probe(mode)
                except Exception:
                    running = None
                try:
                    contract_matches = self._mode_contract_probe(mode)
                except Exception:
                    contract_matches = None
                try:
                    running_modes = self._running_modes_probe()
                except Exception:
                    running_modes = None
                try:
                    required_plane_ready = self._required_plane_probe(mode)
                except Exception:
                    required_plane_ready = None
                if runtime_request_is_already_ready(
                    requested_mode=mode,
                    active_mode=active_mode,
                    running=running,
                    contract_matches=contract_matches,
                    running_modes=running_modes,
                    required_plane_ready=required_plane_ready,
                ):
                    self._phase = "idle"
                    self._requested_mode = None
                    self._message = "Selected runtime is already ready."
                    self._diagnostic_code = None
                    self._last_probe_at = time.monotonic()
                    self._last_probe_mode = mode
                    self._last_probe_running = True
                    self._consecutive_probe_failures = 0
                    return True, self.snapshot()
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
            self._diagnostic_code = None
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
                if mode == "live":
                    # The dashboard controller is long-lived and does not
                    # inherit one-off terminal exports.  Make its Live route
                    # explicit and fail closed to the virtual endpoint unless
                    # a host administrator deliberately opts it into the
                    # external controller at service start.
                    endpoint_source = environment.get(
                        "TASKPLANNER_RUNTIME_CONTROL_LIVE_ROBOT_ENDPOINT_SOURCE",
                        "virtual",
                    ).strip().lower()
                    retraction_endpoint_source = environment.get(
                        "TASKPLANNER_RUNTIME_CONTROL_LIVE_RETRACTION_ENDPOINT_SOURCE",
                        endpoint_source,
                    ).strip().lower()
                    environment["TASKPLANNER_LIVE_ROBOT_ENDPOINT_SOURCE"] = (
                        endpoint_source
                        if endpoint_source in LIVE_ENDPOINT_SOURCES
                        else "virtual"
                    )
                    environment["TASKPLANNER_LIVE_RETRACTION_ENDPOINT_SOURCE"] = (
                        retraction_endpoint_source
                        if retraction_endpoint_source in LIVE_ENDPOINT_SOURCES
                        else environment["TASKPLANNER_LIVE_ROBOT_ENDPOINT_SOURCE"]
                    )
                # The dashboard owns reviewed mode transitions. Ask the
                # launcher to validate its mode-specific install contract and
                # rebuild only when artifacts are stale; a blind --no-build
                # otherwise starts containers with obsolete ROS entry points
                # and fails later as an opaque rosbridge timeout.
                command = [str(self._launcher), "up", mode, "--ensure-build"]
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
                self._diagnostic_code = None
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
                self._diagnostic_code = None
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
                self._diagnostic_code = None
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
        if self.path == "/v1/runtime/asr/status":
            if not self._authorized():
                return
            self._send_json(
                HTTPStatus.OK,
                self.server.controller.asr_restart_snapshot().to_dict(),
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {
            "/v1/runtime/transition",
            "/v1/runtime/asr/restart",
        }:
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
        if self.path == "/v1/runtime/asr/restart":
            if not isinstance(payload, dict) or payload:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "ASR restart request must be an empty object"},
                )
                return
            request_id = self.headers.get(REQUEST_ID_HEADER, "")
            if not REQUEST_ID_RE.fullmatch(request_id):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "a valid ASR restart request ID is required"},
                )
                return
            accepted, snapshot = self.server.controller.start_asr_restart(request_id)
            self._send_json(
                HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT,
                snapshot.to_dict(),
                extra={"accepted": accepted},
            )
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
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except ConnectionError:
            # The transition continues under host authority even if a browser
            # times out or navigates away before receiving the HTTP snapshot.
            # Avoid a misleading server traceback; the client reconciles with
            # the read-only status endpoint after reconnecting.
            return


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
