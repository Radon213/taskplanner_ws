"""ROS-independent health probe for Debug Mode's text-only retractor VLM."""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from procedure_spec import RetractionCommand, RetractionState


HEALTH_PROBE_TRANSCRIPT = "리트렉터 직접 가르치기 모드 켜줘"
HEALTH_PROBE_STATE = RetractionState.IDLE
HEALTH_PROBE_EXPECTED_COMMAND = RetractionCommand.START_DIRECT_TEACH


@dataclass(frozen=True, slots=True)
class VLMRuntimeStatus:
    """Observed manager/catalog state for one launch-fixed model."""

    manager_reachable: bool
    catalog_reachable: bool
    load_state: str
    loaded: bool
    available: bool
    runtime_managed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "manager_reachable": self.manager_reachable,
            "catalog_reachable": self.catalog_reachable,
            "load_state": self.load_state,
            "loaded": self.loaded,
            "available": self.available,
            "runtime_managed": self.runtime_managed,
            "detail": self.detail,
        }


class FixedVLMRuntimeClient:
    """Read/control only the base URL and model fixed at node launch.

    No operation accepts a browser-supplied URL or model identifier.  The API
    key is retained only in private request headers and never appears in a
    returned status or exception detail.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model_id: str,
        api_key: str = "",
        timeout_sec: float = 2.0,
        request_json: Callable[
            [str, str, dict[str, Any] | None, float, dict[str, str]], object
        ]
        | None = None,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.model_id = str(model_id or "").strip()
        self._api_key = str(api_key or "").strip()
        self._timeout_sec = max(0.1, float(timeout_sec))
        self._request_json = request_json or self._urlopen_json

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _urlopen_json(
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        timeout_sec: float,
        headers: dict[str, str],
    ) -> object:
        request = Request(
            url,
            data=(
                json.dumps(payload, separators=(",", ":")).encode("utf-8")
                if payload is not None
                else None
            ),
            headers=headers,
            method=method,
        )
        with urlopen(request, timeout=timeout_sec) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _detail_from_error(exc: Exception) -> str:
        # Never include request headers, URLs with query strings, or response
        # bodies that could echo credentials.
        if isinstance(exc, HTTPError):
            return f"HTTP {exc.code}"
        return type(exc).__name__

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> object:
        if not self.base_url:
            raise ValueError("vlm_base_url_not_configured")
        return self._request_json(
            method,
            f"{self.base_url}{path}",
            payload,
            self._timeout_sec,
            self._headers(),
        )

    def refresh(self) -> VLMRuntimeStatus:
        manager_reachable = False
        try:
            health = self._request("GET", "/health")
            manager_reachable = isinstance(health, dict)
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            return VLMRuntimeStatus(
                manager_reachable=False,
                catalog_reachable=False,
                load_state="offline",
                loaded=False,
                available=False,
                runtime_managed=False,
                detail=f"manager_unreachable:{self._detail_from_error(exc)}",
            )

        try:
            catalog = self._request("GET", "/v1/models")
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            return VLMRuntimeStatus(
                manager_reachable=manager_reachable,
                catalog_reachable=False,
                load_state="unknown",
                loaded=False,
                available=False,
                runtime_managed=False,
                detail=f"catalog_unreachable:{self._detail_from_error(exc)}",
            )
        rows = catalog.get("data", []) if isinstance(catalog, dict) else []
        row = next(
            (
                item
                for item in rows
                if isinstance(item, dict)
                and str(item.get("id", "")).strip() == self.model_id
            ),
            None,
        )
        if row is None:
            return VLMRuntimeStatus(
                manager_reachable=manager_reachable,
                catalog_reachable=True,
                load_state="missing",
                loaded=False,
                available=False,
                runtime_managed=False,
                detail="configured_model_not_in_catalog",
            )
        return VLMRuntimeStatus(
            manager_reachable=manager_reachable,
            catalog_reachable=True,
            load_state=str(row.get("load_state", "unknown") or "unknown"),
            loaded=bool(row.get("loaded", False)),
            available=bool(row.get("available", False)),
            runtime_managed=bool(row.get("runtime_managed", False)),
            detail=str(row.get("detail", "")),
        )

    def load(self) -> VLMRuntimeStatus:
        """Explicitly request loading only after validating the catalog row."""

        current = self.refresh()
        if not current.manager_reachable or not current.catalog_reachable:
            return current
        if not current.runtime_managed or not current.available:
            return VLMRuntimeStatus(
                manager_reachable=current.manager_reachable,
                catalog_reachable=current.catalog_reachable,
                load_state=current.load_state,
                loaded=current.loaded,
                available=current.available,
                runtime_managed=current.runtime_managed,
                detail="configured_model_is_not_loadable",
            )
        if current.loaded or current.load_state == "loading":
            return current
        try:
            payload = self._request(
                "POST",
                "/manager/load",
                {"model_id": self.model_id},
            )
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            return VLMRuntimeStatus(
                manager_reachable=current.manager_reachable,
                catalog_reachable=current.catalog_reachable,
                load_state="error",
                loaded=False,
                available=current.available,
                runtime_managed=current.runtime_managed,
                detail=f"load_failed:{self._detail_from_error(exc)}",
            )
        row = payload if isinstance(payload, dict) else {}
        state = str(row.get("state", "loading") or "loading")
        return VLMRuntimeStatus(
            manager_reachable=True,
            catalog_reachable=True,
            load_state=state,
            loaded=state == "loaded",
            available=current.available,
            runtime_managed=current.runtime_managed,
            detail=str(row.get("detail", "load requested")),
        )


@dataclass(frozen=True, slots=True)
class RetractionVLMHealthResult:
    """Result of one real model interpretation with no ROS command dispatch."""

    healthy: bool
    latency_ms: float
    actual_command: str
    interpreter_source: str
    vlm_invoked: bool
    detail: str
    error_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": "healthy" if self.healthy else "unhealthy",
            "worker_healthy": self.healthy,
            "micro_test_passed": self.healthy,
            "probe_transcript": HEALTH_PROBE_TRANSCRIPT,
            "probe_state": HEALTH_PROBE_STATE.value,
            "expected_command": HEALTH_PROBE_EXPECTED_COMMAND.value,
            "actual_command": self.actual_command,
            "interpreter_source": self.interpreter_source,
            "vlm_invoked": self.vlm_invoked,
            "detail": self.detail,
            "error_type": self.error_type,
            "latency_ms": round(max(0.0, self.latency_ms), 3),
        }


def run_retraction_vlm_health_probe(
    interpreter: Any,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> RetractionVLMHealthResult:
    """Exercise actual model interpretation without calling ROS or a Service.

    A deterministic fallback is deliberately unhealthy here: it proves the
    local parser still works, but it does not prove the configured model worker
    accepted a request and returned the expected grounded command.
    """

    started = monotonic()
    try:
        interpretation = interpreter.interpret(
            HEALTH_PROBE_TRANSCRIPT,
            HEALTH_PROBE_STATE,
        )
        command = interpretation.normalized.command
        actual_command = command.value if command is not None else ""
        healthy = bool(
            interpretation.vlm_invoked
            and interpretation.interpreter_source == "text_vlm"
            and command == HEALTH_PROBE_EXPECTED_COMMAND
        )
        return RetractionVLMHealthResult(
            healthy=healthy,
            latency_ms=(monotonic() - started) * 1_000.0,
            actual_command=actual_command,
            interpreter_source=str(interpretation.interpreter_source),
            vlm_invoked=bool(interpretation.vlm_invoked),
            detail=str(interpretation.detail),
        )
    except Exception as exc:  # health reporting must never stop Debug Mode
        return RetractionVLMHealthResult(
            healthy=False,
            latency_ms=(monotonic() - started) * 1_000.0,
            actual_command="",
            interpreter_source="probe_error",
            vlm_invoked=False,
            detail=f"retraction_vlm_health_probe_error:{type(exc).__name__}",
            error_type=type(exc).__name__,
        )
