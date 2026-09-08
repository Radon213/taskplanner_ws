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
import tomllib
import uuid
from dataclasses import dataclass, asdict, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Final
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

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
# Runtime-mode replacement intentionally does not wait for Digital Twin state,
# scenario receipts, or an executor-wide readiness protocol.  The one runtime
# fact that can block replacement is a request currently owned by the physical
# execution endpoint.  That owner publishes this small, latched projection.
EXECUTION_ROUTE_STATE_TOPIC: Final[str] = "/integration/execution_route/state"
EXECUTION_ROUTE_STATE_TYPE: Final[str] = "std_msgs/msg/String"
EXECUTION_ROUTE_STATE_SCHEMA: Final[str] = "taskplanner.execution_route_state.v1"
EXECUTION_OWNED_MODES: Final[frozenset[str]] = frozenset({"live", "llm-surgeon"})
LIVE_ENDPOINT_SOURCES: Final[frozenset[str]] = frozenset({"virtual", "external"})
RUNTIME_PROFILE_MISMATCH_CODE: Final[str] = "runtime_profile_mismatch"
TASKPLANNER_RUNTIME_MODE_ENV: Final[str] = "TASKPLANNER_RUNTIME_MODE"
# ASR restart is delegated unchanged to `scripts/taskplanner restart asr`.
# This controller intentionally owns only the HTTP job lifecycle, not a second
# Docker/source/ROS-graph implementation of the same restart.
ASR_RESTART_COMMAND_TIMEOUT_SEC: Final[float] = 75.0
OWNER_RESTART_COMMAND_TIMEOUT_SEC: Final[float] = 75.0
SURGIMATE_COMMAND_TIMEOUT_SEC: Final[float] = 30.0
SURGIMATE_ACTIONS: Final[frozenset[str]] = frozenset({"start", "stop", "restart"})
# Runtime lifecycle remains a small, explicit debug/operator capability.  It
# deliberately controls only the one NInfer sidecar and the reviewed launcher
# paths; the browser never receives Docker, shell, or arbitrary model access.
LIFECYCLE_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"ninfer_restart", "qwen_load", "qwen_unload", "warm_restart", "clean_restart"}
)
LIFECYCLE_COMMAND_TIMEOUT_SEC: Final[float] = DEFAULT_TRANSITION_TIMEOUT_SEC
NINFER_MANAGER_REQUEST_TIMEOUT_SEC: Final[float] = 4.0
NINFER_TEXT_MAX_CHARS: Final[int] = 512


RUNTIME_OWNER_REGISTRY_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1] / "config" / "taskplanner_runtime_owners.toml"
)
_OWNER_INVENTORY_LOCK = threading.RLock()
_OWNER_INVENTORY_MTIME_NS: int | None = None
_OWNER_INVENTORY_ERROR: str | None = None


def _default_owner_inventory() -> tuple[
    frozenset[str],
    dict[str, str],
    frozenset[str],
    dict[str, str],
    dict[str, dict[str, str]],
    dict[str, str],
]:
    """Read owner names/modes from the canonical local TOML inventory.

    Runtime control intentionally has no second owner allowlist.  If the
    inventory is absent or malformed, owner restart requests simply remain
    unavailable while mode-control itself can report the problem.
    """

    registry_path = RUNTIME_OWNER_REGISTRY_PATH
    try:
        payload = tomllib.loads(registry_path.read_text(encoding="utf-8"))
        raw_owners = payload.get("owners")
        raw_aliases = payload.get("aliases", {})
        raw_anchors = payload.get("runtime_mode_anchors", {})
        if (
            not isinstance(raw_owners, dict)
            or not isinstance(raw_aliases, dict)
            or not isinstance(raw_anchors, dict)
        ):
            return frozenset(), {}, frozenset(), {}, {}, {}
        names = frozenset(name for name, value in raw_owners.items() if isinstance(value, dict))
        aliases = {
            alias: target
            for alias, target in raw_aliases.items()
            if isinstance(alias, str) and isinstance(target, str) and target in names
        }
        modes = frozenset(
            mode
            for owner in raw_owners.values()
            if isinstance(owner, dict)
            for mode in owner.get("modes", [])
            if isinstance(mode, str)
        )
        owner_services: dict[str, dict[str, str]] = {}
        owner_strategies: dict[str, str] = {}
        for owner_name, owner in raw_owners.items():
            if not isinstance(owner_name, str) or not isinstance(owner, dict):
                return frozenset(), {}, frozenset(), {}, {}, {}
            raw_modes = owner.get("modes", [])
            if not isinstance(raw_modes, list) or not all(
                isinstance(mode, str) for mode in raw_modes
            ):
                return frozenset(), {}, frozenset(), {}, {}, {}
            services = owner.get("services")
            if services is not None and not isinstance(services, dict):
                return frozenset(), {}, frozenset(), {}, {}, {}
            strategy = owner.get("restart_strategy")
            if not isinstance(strategy, str) or not strategy:
                return frozenset(), {}, frozenset(), {}, {}, {}
            owner_strategies[owner_name] = strategy
            owner_services[owner_name] = {}
            for mode in raw_modes:
                service = (
                    services.get(mode)
                    if isinstance(services, dict)
                    else owner.get("service")
                )
                if isinstance(service, str) and service:
                    owner_services[owner_name][mode] = service
        anchors: dict[str, str] = {}
        for mode, owner_name in raw_anchors.items():
            if not isinstance(mode, str) or not isinstance(owner_name, str):
                return frozenset(), {}, frozenset(), {}, {}, {}
            owner = raw_owners.get(owner_name)
            if not isinstance(owner, dict) or mode not in owner.get("modes", []):
                return frozenset(), {}, frozenset(), {}, {}, {}
            service = owner_services.get(owner_name, {}).get(mode)
            if not service:
                return frozenset(), {}, frozenset(), {}, {}, {}
            anchors[mode] = service
        return names, aliases, modes, anchors, owner_services, owner_strategies
    except (OSError, tomllib.TOMLDecodeError):
        return frozenset(), {}, frozenset(), {}, {}, {}


RUNTIME_OWNER_NAMES: frozenset[str] = frozenset()
RUNTIME_OWNER_ALIASES: dict[str, str] = {}
RUNTIME_OWNER_MODES: frozenset[str] = frozenset()
RUNTIME_MODE_ANCHOR_SERVICES: dict[str, str] = {}
RUNTIME_OWNER_SERVICES: dict[str, dict[str, str]] = {}
RUNTIME_OWNER_STRATEGIES: dict[str, str] = {}


def refresh_runtime_owner_inventory() -> bool:
    """Reload the one TOML owner inventory only after an atomic file update.

    The browser controller is deliberately long-lived.  Keeping this tiny
    mtime-aware reload here means a researcher can add/rename an owner in the
    canonical TOML and use it without restarting the runtime-control process.
    A malformed intermediate editor write retains the last known-good
    inventory instead of turning a working control plane into an empty one.
    """

    global _OWNER_INVENTORY_MTIME_NS, _OWNER_INVENTORY_ERROR
    global RUNTIME_OWNER_NAMES, RUNTIME_OWNER_ALIASES, RUNTIME_OWNER_MODES
    global RUNTIME_MODE_ANCHOR_SERVICES, RUNTIME_OWNER_SERVICES
    global RUNTIME_OWNER_STRATEGIES
    try:
        mtime_ns = RUNTIME_OWNER_REGISTRY_PATH.stat().st_mtime_ns
    except OSError as exc:
        _OWNER_INVENTORY_ERROR = str(exc)
        return bool(RUNTIME_OWNER_NAMES)
    with _OWNER_INVENTORY_LOCK:
        if _OWNER_INVENTORY_MTIME_NS == mtime_ns:
            return bool(RUNTIME_OWNER_NAMES)
        inventory = _default_owner_inventory()
        if not inventory[0]:
            _OWNER_INVENTORY_ERROR = "owner inventory is unavailable or invalid"
            return bool(RUNTIME_OWNER_NAMES)
        (
            RUNTIME_OWNER_NAMES,
            RUNTIME_OWNER_ALIASES,
            RUNTIME_OWNER_MODES,
            RUNTIME_MODE_ANCHOR_SERVICES,
            RUNTIME_OWNER_SERVICES,
            RUNTIME_OWNER_STRATEGIES,
        ) = inventory
        _OWNER_INVENTORY_MTIME_NS = mtime_ns
        _OWNER_INVENTORY_ERROR = None
        return True


refresh_runtime_owner_inventory()


def canonical_runtime_owner_name(owner: str) -> str:
    """Return the one TOML-defined owner name for a CLI/API spelling."""

    refresh_runtime_owner_inventory()
    return RUNTIME_OWNER_ALIASES.get(owner, owner)


def runtime_mode_anchor_service(mode: str) -> str | None:
    """Return the TOML-declared service that represents one runtime mode."""

    refresh_runtime_owner_inventory()
    return RUNTIME_MODE_ANCHOR_SERVICES.get(mode)


def runtime_owner_service(owner: str, mode: str) -> str | None:
    """Return one TOML-declared owner service for a selected runtime mode."""

    refresh_runtime_owner_inventory()
    canonical = canonical_runtime_owner_name(owner)
    return RUNTIME_OWNER_SERVICES.get(canonical, {}).get(mode)
CORE_RUNTIME_ROSBRIDGE_MODES: Final[frozenset[str]] = frozenset(
    {"live", "llm-surgeon"}
)
CORE_RUNTIME_ROSBRIDGE_PORT_ENV: Final[str] = "ROSBRIDGE_PORT"
# `taskplanner-state-core` is shared by the Live and LLM Surgeon Compose
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


def runtime_owner_status(root: Path, mode: str) -> list[dict[str, str]]:
    """Return the registry's read-only Docker projection for one mode."""

    refresh_runtime_owner_inventory()
    if mode not in RUNTIME_OWNER_MODES:
        raise ValueError("unsupported runtime owner mode")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(root / "scripts" / "taskplanner_owner_registry.py"),
                "--root",
                str(root),
                "status",
                "all",
                "--mode",
                mode,
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=4.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("runtime owner status is unavailable") from exc
    if result.returncode != 0:
        raise RuntimeError("runtime owner status is unavailable")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("runtime owner status is malformed") from exc
    required = {"owner", "mode", "state", "service", "detail"}
    if not isinstance(payload, list) or any(
        not isinstance(item, dict)
        or set(item) != required
        or not all(isinstance(value, str) for value in item.values())
        for item in payload
    ):
        raise RuntimeError("runtime owner status is malformed")
    return payload


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
class NInferSnapshot:
    """Small, non-secret projection of the locally managed Qwen runtime."""

    available: bool
    model_id: str | None
    model_state: str
    detail: str


@dataclass(frozen=True)
class RuntimeLifecycleSnapshot:
    """One bounded runtime/VLM lifecycle request visible to the dashboard."""

    phase: str
    generation: int
    job_id: str | None
    request_id: str | None
    operation: str | None
    active_mode: str | None
    message: str
    retryable: bool
    ninfer: NInferSnapshot

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AsrRestartError(RuntimeError):
    """Internal error carrying only a reviewed, non-sensitive UI message."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.public_message = message
        self.retryable = retryable


def runtime_request_is_already_ready(
    *,
    requested_mode: str,
    active_mode: str | None,
    running: bool | None,
    contract_matches: bool | None,
    running_modes: set[str] | None,
    owner_plane_ready: bool | None = None,
) -> bool:
    """Return true only for an unambiguous, healthy same-mode request.

    A repeated dashboard selection is not a runtime transition.  It must not
    require the procedure to stop or invoke the launcher, but it may be
    treated as a no-op only while the marker, the service probe, the container
    contract, and independent running-mode discovery all agree. Optional
    sidecars such as ASR, VLM, the browser, and public rosbridge expose their
    own health and restart paths; they must not turn a same-mode selection
    into a core transition.  A concretely missing split runtime owner means
    the profile is not fully converged, so the launcher should perform its
    narrow owner reconciliation instead of describing the request as ready.
    """

    return (
        requested_mode in ALLOWED_MODES
        and active_mode == requested_mode
        and running is True
        and contract_matches is True
        and running_modes == {requested_mode}
        and owner_plane_ready is not False
    )


def runtime_owner_plane_ready(rows: list[dict[str, str]], mode: str) -> bool:
    """Return whether every applicable non-sidecar owner is actually running.

    This is deliberately a no-op accuracy check for the split owner plane,
    not a start gate.  ``False`` makes an otherwise active same-mode request
    fall through to the launcher's narrow owner reconciliation; it must never
    reject a researcher-requested mode transition.  ``asr`` remains
    independently restartable.  The registry emits ``not-applicable`` for
    owners outside a profile, so the same small rule covers Live and LLM
    Surgeon without a second mode-to-owner table here.
    """

    refresh_runtime_owner_inventory()
    # Audio/static sidecars expose their own lifecycle. A stopped optional
    # sidecar must not turn a healthy core request into an implicit runtime
    # reconciliation that revives an explicit operator stop.
    relevant = [
        row
        for row in rows
        if (
            RUNTIME_OWNER_STRATEGIES.get(row.get("owner", "")) not in {"asr", "sidecar"}
            and row.get("state") not in {"not-applicable", "disabled"}
        )
    ]
    return bool(relevant) and all(row.get("state") == "running" for row in relevant)


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
    """Identify the active split state core from its container contract."""

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

    Live and LLM Surgeon share one split operator-bridge service and bind the
    browser bridge directly on its declared loopback port. The
    9091 path router is an optional Ops-plane component, so treating it as a
    core liveness requirement would incorrectly erase a healthy Live marker.
    """

    if mode not in CORE_RUNTIME_ROSBRIDGE_MODES:
        return None
    service = runtime_owner_service("operator-bridge", mode)
    if service is None:
        return None
    try:
        container_id = _running_service_container_id(root, service)
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

    service = runtime_mode_anchor_service(mode)
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


def _running_mode_container_id(root: Path, mode: str) -> str | None:
    service = runtime_mode_anchor_service(mode)
    if service is None:
        return None
    return _running_service_container_id(root, service)


def _running_owner_container_id(root: Path, owner: str, mode: str) -> str | None:
    """Resolve a running owner through the TOML service inventory."""

    service = runtime_owner_service(owner, mode)
    if service is None:
        return None
    return _running_service_container_id(root, service)


def _running_service_container_id(root: Path, service: str) -> str | None:
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
    operational_anchor_services = {
        service
        for mode in ("live", "llm-surgeon")
        if (service := runtime_mode_anchor_service(mode))
    }
    if operational_anchor_services & services:
        # Live and LLM share a core service. Never guess Live from its service
        # name: an accidental Compose recreate otherwise leaves the dashboard
        # in Live while a mock runtime is actually running.
        mode = inspect_taskplanner_runtime_mode(root)
        if mode is None:
            return None
        candidates.add(mode)
    for mode, service in RUNTIME_MODE_ANCHOR_SERVICES.items():
        if mode in {"live", "llm-surgeon"} or service not in services:
            continue
        # A read-only Debug observer may coexist with the selected operational
        # runtime.  It becomes the active standalone Debug anchor only when
        # there is no operational/replay anchor.
        if mode == "debug" and candidates:
            continue
        candidates.add(mode)
    return candidates


def final_transition_interlock_is_safe(
    root: Path,
    state_file: Path,
    expected_mode: str | None,
    *,
    running_modes_probe: Callable[[], set[str] | None] | None = None,
    execution_idle_probe: Callable[[str], bool | None] | None = None,
) -> tuple[bool, str]:
    """Revalidate mode identity and the affected endpoint owner under lock.

    This is deliberately not a global runtime-readiness gate: ASR, VLM,
    cameras, the Digital Twin, and unrelated owners cannot block a mode
    replacement.  Live/LLM route replacement *does* replace the one owner
    that may still be invoking a physical endpoint.  Therefore an unknown
    execution-route sample is not equivalent to an idle one for those modes.
    Replay and Debug have no operational execution owner and remain ungated.
    """

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
    # reads its runtime environment and therefore distinguishes the two modes.
    expected_candidate = expected_mode
    if candidates - {expected_candidate}:
        return False, "a different or additional runtime is now running"
    if candidates and expected_candidate not in candidates:
        return False, "the expected runtime could not be identified"

    try:
        execution_idle = (
            execution_idle_probe(expected_mode)
            if execution_idle_probe is not None
            else execution_owner_is_idle(root, expected_mode)
        )
    except Exception:
        execution_idle = None
    if execution_idle is False:
        return False, "an execution endpoint request is still in flight"
    if execution_idle is True:
        if not candidates:
            return True, "the previous runtime stopped with no execution endpoint request in flight"
        return True, "the active runtime has no execution endpoint request in flight"
    if expected_mode in EXECUTION_OWNED_MODES:
        return False, "execution endpoint activity is unavailable for the active route"
    if not candidates:
        return True, "the previously active runtime has already stopped"
    return True, "the active runtime has no operational execution endpoint owner"


def execution_route_state_is_idle(topic_sample: dict[str, Any]) -> bool | None:
    """Read only the execution owner's in-flight request facts from a sample.

    This parser intentionally ignores route readiness, scenario state, Digital
    Twin receipts, and the owner-local ``restart_allowed`` convenience field.
    Mode replacement only needs to know whether this owner is currently
    invoking an endpoint.
    """

    raw = topic_sample.get("data")
    if not isinstance(raw, str) or len(raw) > 16 * 1024:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("schema") != EXECUTION_ROUTE_STATE_SCHEMA:
        return None
    active_count = payload.get("active_request_count")
    proxy_active = payload.get("execution_proxy_active")
    if (
        not isinstance(active_count, int)
        or isinstance(active_count, bool)
        or active_count < 0
        or not isinstance(proxy_active, bool)
    ):
        return None
    return active_count == 0 and not proxy_active


def execution_owner_is_idle(root: Path, mode: str) -> bool | None:
    """Return False only when the execution owner reports an active request.

    Replay and Debug do not use the operational execution owner.  If the
    owner is not running or its optional status topic cannot be sampled, return
    ``None`` rather than inventing a global readiness requirement.
    """

    if mode not in {"live", "llm-surgeon"}:
        return True
    try:
        container_id = _running_owner_container_id(root, "execution", mode)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if container_id is None:
        return None
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
                "timeout 2 ros2 topic echo --once --no-daemon --spin-time 1 "
                "--timeout 1 --flow-style --full-length \"$1\" \"$2\"",
                "--",
                EXECUTION_ROUTE_STATE_TOPIC,
                EXECUTION_ROUTE_STATE_TYPE,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        topic_sample = next(
            (
                document
                for document in yaml.safe_load_all(result.stdout)
                if document is not None
            ),
            None,
        )
    except yaml.YAMLError:
        return None
    if not isinstance(topic_sample, dict):
        return None
    return execution_route_state_is_idle(topic_sample)


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
        router_port: int = 9091,
        route_paths: dict[str, str] | None = None,
        running_modes_probe: Callable[[], set[str] | None] | None = None,
        owner_status_probe: Callable[[Path, str], list[dict[str, str]]] | None = None,
        command_runner: Callable[..., Any] | None = None,
        asr_restart_command_timeout_sec: float = ASR_RESTART_COMMAND_TIMEOUT_SEC,
    ) -> None:
        if transition_timeout_sec <= 0:
            raise ValueError("transition timeout must be positive")
        if active_probe_ttl_sec < 0:
            raise ValueError("active probe TTL must not be negative")
        if active_probe_failure_threshold < 1:
            raise ValueError("active probe failure threshold must be positive")
        if asr_restart_command_timeout_sec <= 0:
            raise ValueError("ASR restart timeout must be positive")
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
            lambda mode: execution_owner_is_idle(self._root, mode)
        )
        self._mode_contract_probe = mode_contract_probe or (
            lambda mode: mode_runtime_contract_matches(self._root, mode)
        )
        self._running_modes_probe = running_modes_probe or (
            lambda: detect_running_mode_candidates(self._root)
        )
        self._owner_status_probe = owner_status_probe or runtime_owner_status
        self._command_runner = command_runner or subprocess.run
        self._asr_restart_command_timeout_sec = float(
            asr_restart_command_timeout_sec
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
        # Every launcher/owner/model mutation shares this small reservation.
        # Job-specific snapshots still own their UI details, but no two
        # independently exposed HTTP endpoints may mutate the same Compose
        # runtime concurrently.  Status reads deliberately remain lock-free
        # apart from their short snapshot copy.
        self._mutation_operation: str | None = None
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
        self._lifecycle_generation = 0
        self._lifecycle_job_active = False
        self._lifecycle_snapshot = RuntimeLifecycleSnapshot(
            phase="idle",
            generation=0,
            job_id=None,
            request_id=None,
            operation=None,
            active_mode=None,
            message="Runtime lifecycle control is ready.",
            retryable=False,
            ninfer=NInferSnapshot(
                available=False,
                model_id=None,
                model_state="unavailable",
                detail="NInfer manager status has not been read yet.",
            ),
        )

    def _begin_mutation_locked(self, operation: str) -> bool:
        if self._mutation_operation:
            return False
        self._mutation_operation = operation
        return True

    def _finish_mutation(self, operation: str) -> None:
        with self._lock:
            if self._mutation_operation == operation:
                self._mutation_operation = None

    def _mutation_rejection_message_locked(self) -> str:
        operation = self._mutation_operation or "runtime operation"
        return f"Wait for {operation} to finish first."

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

    @staticmethod
    def _ninfer_text(value: object, fallback: str) -> str:
        if not isinstance(value, str):
            return fallback
        normalized = value.strip()
        return normalized[:NINFER_TEXT_MAX_CHARS] if normalized else fallback

    def _ninfer_manager_connection(self) -> tuple[str, dict[str, str]] | None:
        """Resolve the already-running loopback manager without exposing its key."""

        try:
            container_id = _running_service_container_id(self._root, "ninfer-manager")
        except (OSError, subprocess.TimeoutExpired):
            return None
        if container_id is None:
            return None
        environment = _container_environment(container_id)
        if environment is None:
            return None
        try:
            port = int(environment.get("NINFER_MANAGER_PORT", "8080"))
        except ValueError:
            return None
        if not 1 <= port <= 65535:
            return None
        headers = {"Accept": "application/json"}
        api_key = environment.get("NINFER_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # The Compose service uses host networking.  Its configured host can
        # be 0.0.0.0 for bind purposes, but the control plane always reaches it
        # through loopback.
        return f"http://127.0.0.1:{port}", headers

    def _ninfer_manager_request(
        self,
        path: str,
        *,
        payload: dict[str, str] | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        connection = self._ninfer_manager_connection()
        if connection is None:
            return False, None
        base_url, headers = connection
        request_headers = dict(headers)
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(
            f"{base_url}{path}",
            data=body,
            headers=request_headers,
            method="POST" if payload is not None else "GET",
        )
        try:
            with urlopen(request, timeout=NINFER_MANAGER_REQUEST_TIMEOUT_SEC) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError):
            return False, None
        return isinstance(decoded, dict), decoded if isinstance(decoded, dict) else None

    def _ninfer_snapshot(self) -> NInferSnapshot:
        ok, payload = self._ninfer_manager_request("/manager/status")
        if not ok or payload is None:
            return NInferSnapshot(
                available=False,
                model_id=None,
                model_state="unavailable",
                detail="NInfer manager is unavailable.",
            )
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            return NInferSnapshot(
                available=True,
                model_id=None,
                model_state="unavailable",
                detail="NInfer manager returned no reviewed model catalog.",
            )
        models = [item for item in raw_models if isinstance(item, dict)]
        qwen = next(
            (
                item
                for item in models
                if self._ninfer_text(item.get("id"), "").lower().startswith("qwen")
            ),
            None,
        )
        if qwen is None:
            return NInferSnapshot(
                available=True,
                model_id=None,
                model_state="unavailable",
                detail="Configured Qwen model is not present in the NInfer catalog.",
            )
        return NInferSnapshot(
            available=True,
            model_id=self._ninfer_text(qwen.get("id"), "") or None,
            model_state=self._ninfer_text(qwen.get("state"), "unknown"),
            detail=self._ninfer_text(qwen.get("detail"), "No model detail is available."),
        )

    def lifecycle_snapshot(self) -> RuntimeLifecycleSnapshot:
        """Return current lifecycle state plus a fresh manager-only projection."""

        ninfer = self._ninfer_snapshot()
        with self._lock:
            active_mode = self._reconcile_active_mode_locked()
            self._lifecycle_snapshot = replace(
                self._lifecycle_snapshot,
                active_mode=active_mode,
                ninfer=ninfer,
            )
            return self._lifecycle_snapshot

    def _launcher_child_environment(
        self,
        mode: str,
        *,
        expected_active_mode: str | None,
    ) -> dict[str, str]:
        """Return the same bounded launcher environment used by mode selection."""

        environment = os.environ.copy()
        # A terminal/session override must not make a lifecycle request inherit
        # a stale DDS configuration from an earlier launch.
        environment.pop("CYCLONEDDS_URI", None)
        # The long-lived loopback controller remains the supervisor while its
        # launcher child performs a warm or clean cycle.
        environment["TASKPLANNER_RUNTIME_CONTROL_CHILD"] = "1"
        environment["TASKPLANNER_RUNTIME_REQUIRE_EXECUTION_IDLE"] = "1"
        environment["TASKPLANNER_RUNTIME_EXPECTED_ACTIVE_MODE"] = (
            expected_active_mode or ""
        )
        if mode == "live":
            endpoint_source = environment.get(
                "TASKPLANNER_RUNTIME_CONTROL_LIVE_ROBOT_ENDPOINT_SOURCE",
                "external",
            ).strip().lower()
            retraction_endpoint_source = environment.get(
                "TASKPLANNER_RUNTIME_CONTROL_LIVE_RETRACTION_ENDPOINT_SOURCE",
                endpoint_source,
            ).strip().lower()
            environment["TASKPLANNER_LIVE_ROBOT_ENDPOINT_SOURCE"] = (
                endpoint_source if endpoint_source in LIVE_ENDPOINT_SOURCES else "external"
            )
            environment["TASKPLANNER_LIVE_RETRACTION_ENDPOINT_SOURCE"] = (
                retraction_endpoint_source
                if retraction_endpoint_source in LIVE_ENDPOINT_SOURCES
                else environment["TASKPLANNER_LIVE_ROBOT_ENDPOINT_SOURCE"]
            )
        return environment

    def _update_lifecycle(
        self,
        generation: int,
        job_id: str,
        **changes: Any,
    ) -> bool:
        with self._lock:
            if (
                self._lifecycle_snapshot.generation != generation
                or self._lifecycle_snapshot.job_id != job_id
            ):
                return False
            self._lifecycle_snapshot = replace(self._lifecycle_snapshot, **changes)
            return True

    def start_lifecycle(
        self,
        operation: str,
        request_id: str,
    ) -> tuple[bool, RuntimeLifecycleSnapshot]:
        """Queue exactly one reviewed runtime or Qwen lifecycle operation."""

        if operation not in LIFECYCLE_OPERATIONS:
            raise ValueError("unsupported runtime lifecycle operation")
        with self._lock:
            if self._lifecycle_job_active:
                return False, self._lifecycle_snapshot
            if self._mutation_operation:
                return False, replace(
                    self._lifecycle_snapshot,
                    phase="failed",
                    request_id=request_id,
                    operation=operation,
                    message=self._mutation_rejection_message_locked(),
                    retryable=True,
                )
            if self._asr_job_active:
                return False, replace(
                    self._lifecycle_snapshot,
                    phase="failed",
                    request_id=request_id,
                    operation=operation,
                    message="Wait for the ASR node restart to finish first.",
                    retryable=True,
                )
            if self._phase == "starting":
                return False, replace(
                    self._lifecycle_snapshot,
                    phase="failed",
                    request_id=request_id,
                    operation=operation,
                    message="Wait for the active runtime transition to finish first.",
                    retryable=True,
                )

            active_mode = self._reconcile_active_mode_locked()
            if operation in {"ninfer_restart", "warm_restart", "clean_restart"}:
                if active_mode is None:
                    return False, replace(
                        self._lifecycle_snapshot,
                        phase="failed",
                        request_id=request_id,
                        operation=operation,
                        active_mode=None,
                        message="No reviewed active runtime is available for this operation.",
                        retryable=True,
                    )
            if operation in {"warm_restart", "clean_restart"} and active_mode is not None:
                try:
                    execution_idle = self._transition_interlock_probe(active_mode)
                except Exception:
                    execution_idle = None
                if execution_idle is False:
                    return False, replace(
                        self._lifecycle_snapshot,
                        phase="failed",
                        request_id=request_id,
                        operation=operation,
                        active_mode=active_mode,
                        message="An execution endpoint request is in flight. Wait for it to finish first.",
                        retryable=True,
                    )
                if execution_idle is None and active_mode in EXECUTION_OWNED_MODES:
                    return False, replace(
                        self._lifecycle_snapshot,
                        phase="failed",
                        request_id=request_id,
                        operation=operation,
                        active_mode=active_mode,
                        message=(
                            "Execution endpoint activity is unavailable for the active route. "
                            "Retry after its owner reports an idle state."
                        ),
                        retryable=True,
                    )

            self._lifecycle_generation += 1
            generation = self._lifecycle_generation
            job_id = uuid.uuid4().hex
            mutation_operation = f"runtime lifecycle ({operation})"
            # This cannot normally fail while the controller lock is held,
            # but this is a runtime ownership boundary: never rely on an
            # ``assert`` for mutual exclusion because Python may run with
            # optimizations enabled.
            if not self._begin_mutation_locked(mutation_operation):
                return False, replace(
                    self._lifecycle_snapshot,
                    phase="failed",
                    request_id=request_id,
                    operation=operation,
                    active_mode=active_mode,
                    message=self._mutation_rejection_message_locked(),
                    retryable=True,
                )
            self._lifecycle_job_active = True
            self._lifecycle_snapshot = RuntimeLifecycleSnapshot(
                phase="queued",
                generation=generation,
                job_id=job_id,
                request_id=request_id,
                operation=operation,
                active_mode=active_mode,
                message="Runtime lifecycle request is queued.",
                retryable=False,
                ninfer=self._lifecycle_snapshot.ninfer,
            )
            snapshot = self._lifecycle_snapshot
            threading.Thread(
                target=self._run_lifecycle,
                args=(generation, job_id, operation, active_mode, mutation_operation),
                daemon=True,
                name=f"taskplanner-runtime-lifecycle-{operation}-{generation}",
            ).start()
            return True, snapshot

    def _run_lifecycle(
        self,
        generation: int,
        job_id: str,
        operation: str,
        active_mode: str | None,
        mutation_operation: str,
    ) -> None:
        try:
            if operation in {"qwen_load", "qwen_unload"}:
                self._update_lifecycle(
                    generation,
                    job_id,
                    phase="running",
                    message="Requesting the Qwen model lifecycle change.",
                )
                ninfer = self._ninfer_snapshot()
                if not ninfer.available or not ninfer.model_id:
                    raise RuntimeError("NInfer manager or its Qwen model is unavailable.")
                path = "/manager/load" if operation == "qwen_load" else "/manager/unload"
                accepted, response = self._ninfer_manager_request(
                    path,
                    payload={"model_id": ninfer.model_id},
                )
                if not accepted or response is None:
                    raise RuntimeError("The NInfer manager rejected the Qwen lifecycle request.")
                state = self._ninfer_text(response.get("state"), "requested")
                self._update_lifecycle(
                    generation,
                    job_id,
                    phase="succeeded",
                    message=f"Qwen model {state}.",
                    retryable=False,
                    ninfer=self._ninfer_snapshot(),
                )
                return

            if operation == "ninfer_restart":
                self._update_lifecycle(
                    generation,
                    job_id,
                    phase="running",
                    message="Restarting the NInfer manager only.",
                )
                try:
                    container_id = _running_service_container_id(self._root, "ninfer-manager")
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise RuntimeError("NInfer manager is unavailable.") from error
                if container_id is None:
                    raise RuntimeError("NInfer manager is not running.")
                result = self._command_runner(
                    ["docker", "restart", container_id],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=OWNER_RESTART_COMMAND_TIMEOUT_SEC,
                )
                if result.returncode != 0:
                    raise RuntimeError("The NInfer manager restart failed.")
                self._update_lifecycle(
                    generation,
                    job_id,
                    phase="succeeded",
                    message="NInfer manager restarted.",
                    retryable=False,
                    ninfer=self._ninfer_snapshot(),
                )
                return

            if active_mode is None:
                raise RuntimeError("No reviewed active runtime is available.")
            environment = self._launcher_child_environment(
                active_mode,
                expected_active_mode=active_mode,
            )
            if operation == "warm_restart":
                self._update_lifecycle(
                    generation,
                    job_id,
                    phase="running",
                    message="Warm-restarting the active runtime core.",
                )
                commands = [[str(self._launcher), "up", active_mode]]
            elif operation == "clean_restart":
                self._update_lifecycle(
                    generation,
                    job_id,
                    phase="running",
                    message="Clean-restarting the active runtime.",
                )
                commands = [
                    [str(self._launcher), "down"],
                    [str(self._launcher), "up", active_mode],
                ]
            else:
                raise RuntimeError("Unsupported runtime lifecycle operation.")

            for command in commands:
                result = self._command_runner(
                    command,
                    cwd=str(self._root),
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=LIFECYCLE_COMMAND_TIMEOUT_SEC,
                )
                if result.returncode != 0:
                    raise RuntimeError("The runtime lifecycle command failed.")
            self._update_lifecycle(
                generation,
                job_id,
                phase="succeeded",
                active_mode=active_mode,
                message=(
                    "Runtime warm restart completed."
                    if operation == "warm_restart"
                    else "Runtime clean restart completed."
                ),
                retryable=False,
                ninfer=self._ninfer_snapshot(),
            )
        except (OSError, subprocess.TimeoutExpired, RuntimeError):
            self._update_lifecycle(
                generation,
                job_id,
                phase="failed",
                message="The runtime lifecycle request failed. Review the host status and retry.",
                retryable=True,
                ninfer=self._ninfer_snapshot(),
            )
        except Exception:
            self._update_lifecycle(
                generation,
                job_id,
                phase="failed",
                message="The runtime lifecycle request failed unexpectedly.",
                retryable=True,
                ninfer=self._ninfer_snapshot(),
            )
        finally:
            with self._lock:
                if (
                    self._lifecycle_snapshot.generation == generation
                    and self._lifecycle_snapshot.job_id == job_id
                ):
                    self._lifecycle_job_active = False
            self._finish_mutation(mutation_operation)

    def owner_status(self, mode: str) -> list[dict[str, str]]:
        """Return only owners that this generic restart surface can address.

        The registry CLI intentionally projects every known owner so an
        operator can audit the complete matrix.  The browser-facing generic
        restart surface is different: an owner outside the selected runtime
        has no Compose service for that mode, and rendering it merely creates
        a ``service unspecified`` row with a button that must fail.  Keep
        those rows in the CLI/audit projection, but do not expose them as
        restart targets here.

        SurgiMate remains the one explicit exception: it has its own
        start/stop/restart lifecycle API rather than a generic owner restart.
        Other registry-owned sidecars, including the audio-only TTS owner,
        use the same bounded owner restart API as dedicated ROS owners.
        """

        return [
            row
            for row in self._owner_status_probe(self._root, mode)
            if (
                row.get("owner") != "surgimate"
                and row.get("state") not in {"not-applicable", "disabled"}
                and bool(row.get("service"))
            )
        ]

    def surgimate_status(self) -> dict[str, str]:
        """Return the SurgiMate projection for its active owner mode.

        The monitor is a dedicated sidecar, rather than a generic ROS-owner
        restart target.  It can sit beside either standalone Debug or the
        active Live runtime; the latter is what integrated Debug observes.
        """

        snapshot = self.snapshot()
        active_mode = snapshot.active_mode
        if snapshot.phase != "idle" or active_mode not in {"debug", "live"}:
            raise RuntimeError("SurgiMate sidecar status is unavailable")

        matches = [
            row
            for row in self._owner_status_probe(self._root, active_mode)
            if row.get("owner") == "surgimate" and row.get("mode") == active_mode
        ]
        if len(matches) != 1:
            raise RuntimeError("SurgiMate sidecar status is unavailable")
        return matches[0]

    def control_surgimate(self, action: str) -> tuple[bool, str]:
        """Run the bounded SurgiMate lifecycle for Debug or integrated Live.

        Standalone Debug owns its full start/stop/restart lifecycle.  While
        Debug observes an active Live runtime, SurgiMate is already part of
        that mode and only its scoped sidecar restart is meaningful.  Do not
        route this exception through generic owner restart: its dedicated
        lifecycle surface remains the one owner of start/stop semantics.
        """

        if action not in SURGIMATE_ACTIONS:
            raise ValueError("unsupported SurgiMate action")
        with self._lock:
            if self._mutation_operation:
                return False, self._mutation_rejection_message_locked()
            active_mode = self._reconcile_active_mode_locked()
            if self._phase != "idle" or active_mode not in {"debug", "live"}:
                return False, (
                    "SurgiMate lifecycle is available only while standalone Debug "
                    "or integrated Debug with Live is active."
                )
            if active_mode == "live" and action != "restart":
                return False, "SurgiMate can only be restarted while Live is active."
            mutation_operation = f"SurgiMate {action}"
            if not self._begin_mutation_locked(mutation_operation):
                return False, self._mutation_rejection_message_locked()
        try:
            try:
                command = (
                    [str(self._launcher), "restart", "surgimate", "live"]
                    if active_mode == "live"
                    else [str(self._launcher), "surgimate", action, "--mode", "debug"]
                )
                result = self._command_runner(
                    command,
                    cwd=str(self._root),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=SURGIMATE_COMMAND_TIMEOUT_SEC,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False, "The SurgiMate lifecycle request did not complete."
            if result.returncode != 0:
                return False, "The SurgiMate lifecycle request was rejected or failed."
            messages = {
                "start": "SurgiMate started.",
                "stop": "SurgiMate stopped.",
                "restart": "SurgiMate restarted.",
            }
            return True, messages[action]
        finally:
            self._finish_mutation(mutation_operation)

    def restart_owner(self, owner: str, mode: str) -> tuple[bool, str]:
        """Run the same bounded owner restart command exposed in the shell."""

        owner = canonical_runtime_owner_name(owner)
        if owner not in RUNTIME_OWNER_NAMES or mode not in RUNTIME_OWNER_MODES:
            raise ValueError("unsupported runtime owner or mode")
        if owner == "surgimate":
            raise ValueError("SurgiMate uses its dedicated lifecycle endpoint")
        with self._lock:
            if self._mutation_operation:
                return False, self._mutation_rejection_message_locked()
            if self._phase != "idle" or self._reconcile_active_mode_locked() != mode:
                return False, "The requested runtime mode is not active and ready."
            command = [str(self._launcher), "restart", owner, mode]
            if owner == "asr":
                command.append("--require-active-live")
            mutation_operation = f"owner restart ({owner})"
            if not self._begin_mutation_locked(mutation_operation):
                return False, self._mutation_rejection_message_locked()
        try:
            try:
                result = self._command_runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=OWNER_RESTART_COMMAND_TIMEOUT_SEC,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False, "The owner restart did not complete."
            if result.returncode != 0:
                return False, "The owner restart was rejected or failed."
            return True, "The owner restarted."
        finally:
            self._finish_mutation(mutation_operation)

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
            if self._mutation_operation:
                return False, AsrRestartSnapshot(
                    phase="failed",
                    generation=self._asr_generation,
                    job_id=None,
                    request_id=normalized_request_id,
                    message=self._mutation_rejection_message_locked(),
                    retryable=True,
                    source_revision=None,
                    container_started_at=None,
                    before_pid=None,
                    after_pid=None,
                )
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
            mutation_operation = "ASR node restart"
            if not self._begin_mutation_locked(mutation_operation):
                return False, AsrRestartSnapshot(
                    phase="failed",
                    generation=self._asr_generation,
                    job_id=None,
                    request_id=normalized_request_id,
                    message=self._mutation_rejection_message_locked(),
                    retryable=True,
                    source_revision=None,
                    container_started_at=None,
                    before_pid=None,
                    after_pid=None,
                )
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
                args=(generation, job_id, mutation_operation),
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

    def _run_asr_restart(
        self,
        generation: int,
        job_id: str,
        mutation_operation: str,
    ) -> None:
        try:
            # The dashboard is a client of the same owner command used in a
            # terminal.  Do not duplicate Docker identity checks, Python cache
            # handling, source manifests, or ROS graph probes here: those made
            # one small ASR edit slower and gave the UI a second restart
            # implementation to maintain.
            self._update_asr_restart(
                generation,
                job_id,
                phase="restarting",
                message="Restarting the ASR owner.",
            )
            with self._lock:
                if not self._authoritative_live_mode_locked():
                    raise AsrRestartError(
                        "The authoritative Live runtime changed before ASR restart."
                    )
            try:
                result = self._command_runner(
                    [str(self._launcher), "restart", "asr", "--require-active-live"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self._asr_restart_command_timeout_sec,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise AsrRestartError(
                    "The ASR owner restart did not complete."
                ) from error
            if result.returncode != 0:
                raise AsrRestartError("The ASR owner restart failed.")
            self._update_asr_restart(
                generation,
                job_id,
                phase="succeeded",
                message="The ASR owner restarted.",
                retryable=False,
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
            self._finish_mutation(mutation_operation)

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
        # A reviewed launcher invocation can repair a runtime after an earlier
        # controller-side transition failed.  The marker is written only after
        # semantic readiness, so it is more recent authority than this
        # controller's in-memory failed request.  Accept it only when the
        # marker, core contract, and exactly one discovered runtime agree;
        # otherwise retain the fail-closed status.
        if self._phase == "failed":
            try:
                recovered_running = self._mode_running_probe(active_mode)
                recovered_contract = self._mode_contract_probe(active_mode)
                recovered_candidates = self._running_modes_probe()
            except Exception:
                recovered_running = None
                recovered_contract = None
                recovered_candidates = None
            if (
                recovered_running is True
                and recovered_contract is True
                and recovered_candidates == {active_mode}
            ):
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
        refresh_runtime_owner_inventory()

        with self._lock:
            if self._phase == "starting":
                return False, self.snapshot()
            if self._mutation_operation:
                return False, TransitionSnapshot(
                    phase=self._phase,
                    active_mode=read_active_mode(self._state_file),
                    requested_mode=self._requested_mode,
                    message=self._mutation_rejection_message_locked(),
                    retryable=True,
                    diagnostic_code=self._diagnostic_code,
                )
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
                owner_plane_ready: bool | None = None
                if mode in RUNTIME_OWNER_MODES:
                    try:
                        owner_plane_ready = runtime_owner_plane_ready(
                            self._owner_status_probe(self._root, mode), mode
                        )
                    except Exception:
                        # Docker/registry observation failure is not a second
                        # global readiness barrier.  The core contract probes
                        # above remain authoritative for this same-mode path.
                        owner_plane_ready = None
                if runtime_request_is_already_ready(
                    requested_mode=mode,
                    active_mode=active_mode,
                    running=running,
                    contract_matches=contract_matches,
                    running_modes=running_modes,
                    owner_plane_ready=owner_plane_ready,
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
                    execution_idle = self._transition_interlock_probe(active_mode)
                except Exception:
                    execution_idle = None
                if execution_idle is False:
                    self._requested_mode = mode
                    self._message = (
                        "An execution endpoint request is in flight. "
                        "Wait for it to finish before switching modes."
                    )
                    return False, self.snapshot()
                if execution_idle is None and active_mode in EXECUTION_OWNED_MODES:
                    # This is intentionally scoped to the endpoint owner that
                    # a Live/LLM replacement would stop.  It does not turn
                    # ASR, VLM, camera, or Digital-Twin observation loss into
                    # a global readiness gate.
                    self._requested_mode = mode
                    self._message = (
                        "Execution endpoint activity is unavailable for the active route. "
                        "Retry after its owner reports an idle state."
                    )
                    return False, self.snapshot()

            mutation_operation = f"runtime transition ({mode})"
            if not self._begin_mutation_locked(mutation_operation):
                return False, TransitionSnapshot(
                    phase=self._phase,
                    active_mode=read_active_mode(self._state_file),
                    requested_mode=self._requested_mode,
                    message=self._mutation_rejection_message_locked(),
                    retryable=True,
                    diagnostic_code=self._diagnostic_code,
                )
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
                # Recheck the same active execution owner after the child
                # takes the launcher lock, immediately before marker clear/stop.
                environment["TASKPLANNER_RUNTIME_REQUIRE_EXECUTION_IDLE"] = "1"
                environment["TASKPLANNER_RUNTIME_EXPECTED_ACTIVE_MODE"] = (
                    active_mode or ""
                )
                if mode == "live":
                    # The dashboard controller is long-lived and does not
                    # inherit one-off terminal exports.  Make its Live route
                    # explicit and match the declarative Live profile:
                    # external unless the reviewed controller environment
                    # explicitly selects a virtual endpoint.
                    endpoint_source = environment.get(
                        "TASKPLANNER_RUNTIME_CONTROL_LIVE_ROBOT_ENDPOINT_SOURCE",
                        "external",
                    ).strip().lower()
                    retraction_endpoint_source = environment.get(
                        "TASKPLANNER_RUNTIME_CONTROL_LIVE_RETRACTION_ENDPOINT_SOURCE",
                        endpoint_source,
                    ).strip().lower()
                    environment["TASKPLANNER_LIVE_ROBOT_ENDPOINT_SOURCE"] = (
                        endpoint_source
                        if endpoint_source in LIVE_ENDPOINT_SOURCES
                        else "external"
                    )
                    environment["TASKPLANNER_LIVE_RETRACTION_ENDPOINT_SOURCE"] = (
                        retraction_endpoint_source
                        if retraction_endpoint_source in LIVE_ENDPOINT_SOURCES
                        else environment["TASKPLANNER_LIVE_ROBOT_ENDPOINT_SOURCE"]
                    )
                # Dashboard transitions are the ordinary warm path. ABI/IDL
                # or installed-entrypoint changes use the explicit scoped
                # build path in the CLI; selecting a mode must not trigger a
                # package census or colcon build for a source/config edit.
                # The launcher owns narrow same-mode reconciliation.
                command = [str(self._launcher), "up", mode]
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
                self._mutation_operation = None
                return False, self.snapshot()

            process = self._process
            threading.Thread(
                target=self._wait_for_transition,
                args=(process, mode, mutation_operation),
                daemon=True,
                name="taskplanner-runtime-transition",
            ).start()
            return True, self.snapshot()

    def _wait_for_transition(
        self,
        process: Any,
        mode: str,
        mutation_operation: str,
    ) -> None:
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
            if self._mutation_operation == mutation_operation:
                self._mutation_operation = None
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
        parsed = urlsplit(self.path)
        if parsed.path == "/health" and not parsed.query:
            self._send_json(
                HTTPStatus.OK,
                {"service": "taskplanner-runtime-control", "ready": True},
                # The launcher compares this loaded fingerprint with the
                # current source before deciding that an existing transient
                # service is current.
                extra={"code_fingerprint": LOADED_CODE_FINGERPRINT},
            )
            return
        if parsed.path == "/v1/runtime/status" and not parsed.query:
            if not self._authorized():
                return
            self._send_json(HTTPStatus.OK, self.server.controller.snapshot().to_dict())
            return
        if parsed.path == "/v1/runtime/asr/status" and not parsed.query:
            if not self._authorized():
                return
            self._send_json(
                HTTPStatus.OK,
                self.server.controller.asr_restart_snapshot().to_dict(),
            )
            return
        if parsed.path == "/v1/runtime/lifecycle" and not parsed.query:
            if not self._authorized():
                return
            self._send_json(
                HTTPStatus.OK,
                self.server.controller.lifecycle_snapshot().to_dict(),
            )
            return
        if parsed.path == "/v1/runtime/surgimate" and not parsed.query:
            if not self._authorized():
                return
            try:
                status = self.server.controller.surgimate_status()
            except RuntimeError:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "SurgiMate sidecar status is unavailable"},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "active_mode": self.server.controller.snapshot().active_mode,
                    "status": status,
                },
            )
            return
        if parsed.path == "/v1/runtime/owners":
            if not self._authorized():
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            if set(query) != {"mode"} or len(query["mode"]) != 1:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "owner status requires exactly one runtime mode"},
                )
                return
            mode = query["mode"][0]
            try:
                owners = self.server.controller.owner_status(mode)
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "unsupported runtime owner mode"},
                )
                return
            except RuntimeError:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "runtime owner status is unavailable"},
                )
                return
            self._send_json(HTTPStatus.OK, {"mode": mode, "owners": owners})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.query or parsed.path not in {
            "/v1/runtime/transition",
            "/v1/runtime/asr/restart",
            "/v1/runtime/lifecycle",
            "/v1/runtime/surgimate",
            "/v1/runtime/owners/restart",
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
        if parsed.path == "/v1/runtime/asr/restart":
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
        if parsed.path == "/v1/runtime/lifecycle":
            if (
                not isinstance(payload, dict)
                or set(payload) != {"operation"}
                or not isinstance(payload["operation"], str)
            ):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "lifecycle request must contain only one operation"},
                )
                return
            request_id = self.headers.get(REQUEST_ID_HEADER, "")
            if not REQUEST_ID_RE.fullmatch(request_id):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "a valid lifecycle request ID is required"},
                )
                return
            try:
                accepted, snapshot = self.server.controller.start_lifecycle(
                    payload["operation"], request_id
                )
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "unsupported runtime lifecycle operation"},
                )
                return
            self._send_json(
                HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT,
                snapshot.to_dict(),
                extra={"accepted": accepted},
            )
            return
        if parsed.path == "/v1/runtime/surgimate":
            if (
                not isinstance(payload, dict)
                or set(payload) != {"action"}
                or not isinstance(payload["action"], str)
            ):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "SurgiMate request must contain only one action"},
                )
                return
            request_id = self.headers.get(REQUEST_ID_HEADER, "")
            if not REQUEST_ID_RE.fullmatch(request_id):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "a valid SurgiMate request ID is required"},
                )
                return
            action = payload["action"]
            try:
                accepted, message = self.server.controller.control_surgimate(action)
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "unsupported SurgiMate action"},
                )
                return
            body: dict[str, Any] = {"accepted": accepted, "action": action}
            body["message" if accepted else "error"] = message
            self._send_json(HTTPStatus.OK if accepted else HTTPStatus.CONFLICT, body)
            return
        if parsed.path == "/v1/runtime/owners/restart":
            if (
                not isinstance(payload, dict)
                or set(payload) != {"owner", "mode"}
                or not isinstance(payload["owner"], str)
                or not isinstance(payload["mode"], str)
            ):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "owner restart request must contain only owner and mode"},
                )
                return
            request_id = self.headers.get(REQUEST_ID_HEADER, "")
            if not REQUEST_ID_RE.fullmatch(request_id):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "a valid owner restart request ID is required"},
                )
                return
            owner = payload["owner"]
            mode = payload["mode"]
            try:
                accepted, message = self.server.controller.restart_owner(owner, mode)
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "unsupported runtime owner or mode"},
                )
                return
            body: dict[str, Any] = {
                "accepted": accepted,
                "owner": owner,
                "mode": mode,
            }
            body["message" if accepted else "error"] = message
            self._send_json(
                HTTPStatus.OK if accepted else HTTPStatus.CONFLICT,
                body,
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
