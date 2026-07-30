"""Discovery for local OpenAI-compatible model providers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import shlex
import subprocess
import threading
import time
from typing import Any, Callable, Iterable

import requests


@dataclass(frozen=True)
class ConfiguredModel:
    """A model known locally even when its serving endpoint is offline."""

    model_id: str
    display_name: str = ""
    capability: str = "unknown"
    artifact_path: str = ""
    start_command: tuple[str, ...] = ()
    stop_command: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    selectable: bool = True
    detail: str = ""


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    display_name: str
    base_url: str
    api_key: str = ""
    enabled: bool = True
    managed: bool = False
    configured_models: tuple[ConfiguredModel, ...] = ()
    configuration_error: str = ""
    manager_mode: bool = False

    @property
    def models_url(self) -> str:
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            return f"{base_url}/models"
        return f"{base_url}/v1/models"

    @property
    def models_urls(self) -> tuple[str, ...]:
        if self.provider_id != "lmstudio":
            return (self.models_url,)
        base_url = self.base_url.rstrip("/")
        root_url = base_url[:-3] if base_url.endswith("/v1") else base_url
        native_url = f"{root_url}/api/v1/models"
        return (native_url, self.models_url)

    @property
    def manager_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/manager"

    @property
    def lifecycle_kind(self) -> str:
        if not self.managed:
            return "external"
        if self.provider_id == "lmstudio":
            return "lmstudio_native"
        if self.provider_id == "unsloth":
            return "unsloth_native"
        if self.provider_id == "vllm":
            return "vllm_manager"
        if self.provider_id == "ninfer":
            return (
                "ninfer_manager"
                if self.manager_mode
                else "ninfer_process"
            )
        return "external"

    @property
    def runtime_commands(self) -> tuple[str, ...]:
        if self.lifecycle_kind == "vllm_manager":
            return ("load", "unload", "sleep", "wake")
        if self.lifecycle_kind == "ninfer_manager":
            return ("load", "unload")
        if self.lifecycle_kind in {"lmstudio_native", "unsloth_native"}:
            return ("load", "unload")
        if self.lifecycle_kind == "ninfer_process" and self.configured_models:
            return ("load", "unload")
        return ()


@dataclass(frozen=True)
class CatalogModel:
    provider_id: str
    provider_name: str
    model_id: str
    display_name: str
    capability: str
    load_state: str
    selectable: bool
    detail: str = ""
    runtime_managed: bool = False
    available_actions: tuple[str, ...] = ()
    installed: bool = True
    available: bool = True


@dataclass(frozen=True)
class ProviderProbe:
    provider: ProviderConfig
    reachable: bool
    status: str
    detail: str
    latency_sec: float
    models: tuple[CatalogModel, ...] = ()


@dataclass(frozen=True)
class RuntimeControlResult:
    success: bool
    provider_id: str
    model_id: str
    state: str
    message: str


@dataclass(frozen=True)
class _RuntimeOverride:
    state: str
    detail: str
    updated_at: float


RequestGet = Callable[..., Any]
RequestPost = Callable[..., Any]
ProcessPopen = Callable[..., Any]
ProcessRun = Callable[..., Any]

_TRANSITIONAL_STATES = {"loading", "suspending", "waking", "unloading"}


class _LifecycleUnsupported(RuntimeError):
    pass


def _normalized_url(value: str) -> str:
    return value.strip().rstrip("/")


def _env_enabled(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _first_text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _capability(row: dict[str, Any], model_id: str) -> str:
    values: list[str] = []
    for key in ("capability", "capabilities", "modalities", "input_modalities", "type", "task"):
        value = row.get(key)
        if isinstance(value, dict):
            values.extend(
                str(item_key).lower()
                for item_key, enabled in value.items()
                if bool(enabled)
            )
        elif isinstance(value, (list, tuple, set)):
            values.extend(str(item).lower() for item in value)
        elif value is not None:
            values.append(str(value).lower())
    combined = " ".join(values)
    lowered_id = model_id.lower()
    if any(token in combined for token in ("image", "vision", "multimodal")):
        return "vision"
    if "embedding" in combined or "embedding" in lowered_id:
        return "embedding"
    if "rerank" in combined or "rerank" in lowered_id:
        return "reranker"
    if any(token in combined for token in ("text", "chat", "completion", "generate", "llm")):
        return "text"
    return "unknown"


def _load_state(row: dict[str, Any]) -> str:
    state = (
        _first_text(row, "load_state", "state", "status")
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    aliases = {
        "loaded": "loaded",
        "ready": "loaded",
        "running": "loaded",
        "active": "loaded",
        "loading": "loading",
        "starting": "loading",
        "initializing": "loading",
        "sleeping": "sleeping",
        "asleep": "sleeping",
        "suspending": "suspending",
        "waking": "waking",
        "resuming": "waking",
        "unloading": "unloading",
        "stopping": "unloading",
        "error": "error",
        "failed": "error",
        "unloaded": "unloaded",
        "stopped": "unloaded",
        "inactive": "unloaded",
        "not_loaded": "unloaded",
        "available": "unloaded",
        "installed": "unloaded",
    }
    if state in aliases:
        return aliases[state]
    loaded = row.get("loaded")
    if isinstance(loaded, bool):
        return "loaded" if loaded else "unloaded"
    loaded_instances = row.get("loaded_instances")
    if isinstance(loaded_instances, list):
        return "loaded" if loaded_instances else "unloaded"
    return "unknown"


def _bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "available", "installed"}:
            return True
        if normalized in {"0", "false", "no", "off", "unavailable", "missing"}:
            return False
    return default


def _rows_from_payload(payload: Any) -> Iterable[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        rows = payload.get("data", payload.get("models", []))
        if isinstance(rows, list):
            return rows
    return ()


def _unsloth_model_parts(model_id: str) -> tuple[str, str]:
    value = model_id.strip()
    if ":" not in value:
        return value, ""
    model_path, variant = value.rsplit(":", 1)
    if not model_path or not variant or "gguf" not in model_path.lower():
        return value, ""
    return model_path, variant


def _canonical_model_id(provider_id: str, model_id: str) -> str:
    if provider_id == "unsloth":
        return _unsloth_model_parts(model_id)[0]
    return model_id.strip()


def _command_parts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(shlex.split(value))
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _environment_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(
        (str(key), str(item))
        for key, item in value.items()
        if str(key).strip()
    )


def _configured_model_from_row(row: dict[str, Any]) -> ConfiguredModel:
    model_id = _first_text(row, "id", "model_id", "model", "name")
    if not model_id:
        raise ValueError("Configured model is missing id")
    artifact_path = _first_text(row, "artifact_path", "artifact", "path")
    capability = _first_text(row, "capability") or _capability(row, model_id)
    if capability == "unknown" and bool(row.get("vision")):
        capability = "vision"
    return ConfiguredModel(
        model_id=model_id,
        display_name=_first_text(row, "display_name", "name") or model_id,
        capability=capability,
        artifact_path=os.path.expanduser(artifact_path) if artifact_path else "",
        start_command=_command_parts(row.get("start_command")),
        stop_command=_command_parts(row.get("stop_command")),
        environment=_environment_pairs(row.get("environment")),
        selectable=bool(row.get("selectable", True)),
        detail=_first_text(row, "detail"),
    )


def _load_configured_models(
    provider_id: str,
) -> tuple[tuple[ConfiguredModel, ...], str]:
    prefix = provider_id.upper()
    raw_json = os.environ.get(f"{prefix}_MODEL_CATALOG_JSON", "").strip()
    catalog_path = os.environ.get(f"{prefix}_MODEL_CATALOG_PATH", "").strip()
    try:
        if raw_json:
            payload: Any = json.loads(raw_json)
        elif catalog_path:
            payload = json.loads(
                Path(os.path.expanduser(catalog_path)).read_text(encoding="utf-8")
            )
        else:
            model_id = os.environ.get(f"{prefix}_MODEL_ID", "").strip()
            artifact_path = os.environ.get(
                f"{prefix}_MODEL_ARTIFACT",
                "",
            ).strip()
            start_command = os.environ.get(
                f"{prefix}_START_COMMAND",
                "",
            ).strip()
            stop_command = os.environ.get(
                f"{prefix}_STOP_COMMAND",
                "",
            ).strip()
            if not any((model_id, artifact_path, start_command, stop_command)):
                return (), ""
            payload = [
                {
                    "id": model_id,
                    "display_name": os.environ.get(
                        f"{prefix}_MODEL_DISPLAY_NAME",
                        model_id,
                    ),
                    "capability": os.environ.get(
                        f"{prefix}_MODEL_CAPABILITY",
                        "vision",
                    ),
                    "artifact_path": artifact_path,
                    "start_command": start_command,
                    "stop_command": stop_command,
                }
            ]
        rows = payload.get("models", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("Configured model catalog must be a list or {models: [...]}")
        models = tuple(
            _configured_model_from_row(row)
            for row in rows
            if isinstance(row, dict)
        )
        return models, ""
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return (), f"Invalid {provider_id} model catalog: {exc}"


class ModelProviderRegistry:
    """Queries independent local providers without coupling their failures."""

    def __init__(
        self,
        providers: Iterable[ProviderConfig],
        *,
        timeout_sec: float = 2.5,
        runtime_timeout_sec: float = 900.0,
        request_get: RequestGet = requests.get,
        request_post: RequestPost = requests.post,
        process_popen: ProcessPopen = subprocess.Popen,
        process_run: ProcessRun = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._providers = tuple(provider for provider in providers if provider.enabled)
        self._provider_by_id = {provider.provider_id: provider for provider in self._providers}
        self._timeout_sec = max(0.2, float(timeout_sec))
        self._runtime_timeout_sec = max(
            self._timeout_sec,
            float(runtime_timeout_sec),
        )
        self._request_get = request_get
        self._request_post = request_post
        self._process_popen = process_popen
        self._process_run = process_run
        self._sleep = sleep
        self._runtime_lock = threading.RLock()
        self._runtime_overrides: dict[tuple[str, str], _RuntimeOverride] = {}
        self._runtime_threads: dict[tuple[str, str], threading.Thread] = {}
        self._runtime_processes: dict[tuple[str, str], Any] = {}
        self._runtime_support: dict[str, bool] = {
            provider.provider_id: bool(provider.runtime_commands)
            for provider in self._providers
        }

    @classmethod
    def from_environment(
        cls,
        *,
        legacy_base_url: str = "",
        legacy_api_key: str = "",
        request_get: RequestGet = requests.get,
        request_post: RequestPost = requests.post,
        process_popen: ProcessPopen = subprocess.Popen,
        process_run: ProcessRun = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> "ModelProviderRegistry":
        timeout_sec = float(os.environ.get("MODEL_PROVIDER_DISCOVERY_TIMEOUT_SEC", "2.5"))
        runtime_timeout_sec = float(
            os.environ.get("MODEL_PROVIDER_RUNTIME_TIMEOUT_SEC", "900")
        )
        ninfer_models, ninfer_configuration_error = _load_configured_models(
            "ninfer"
        )
        specs = (
            (
                "lmstudio",
                "LM Studio",
                "LMSTUDIO_BASE_URL",
                "http://127.0.0.1:1234",
                "LMSTUDIO_API_KEY",
                "LMSTUDIO_PROVIDER_ENABLED",
                "LMSTUDIO_PROVIDER_MANAGED",
            ),
            (
                "unsloth",
                "Unsloth Studio",
                "UNSLOTH_BASE_URL",
                "http://127.0.0.1:8888",
                "UNSLOTH_API_KEY",
                "UNSLOTH_PROVIDER_ENABLED",
                "UNSLOTH_PROVIDER_MANAGED",
            ),
            (
                "vllm",
                "vLLM",
                "VLLM_BASE_URL",
                "http://127.0.0.1:8001",
                "VLLM_API_KEY",
                "VLLM_PROVIDER_ENABLED",
                "VLLM_PROVIDER_MANAGED",
            ),
            (
                "ninfer",
                "NInfer",
                "NINFER_BASE_URL",
                "http://127.0.0.1:8080",
                "NINFER_API_KEY",
                "NINFER_PROVIDER_ENABLED",
                "NINFER_PROVIDER_MANAGED",
            ),
        )
        legacy_url = _normalized_url(legacy_base_url)
        providers: list[ProviderConfig] = []
        for (
            provider_id,
            display_name,
            url_env,
            default_url,
            key_env,
            enabled_env,
            managed_env,
        ) in specs:
            base_url = _normalized_url(os.environ.get(url_env, default_url))
            api_key = os.environ.get(key_env, "").strip()
            if not api_key and legacy_api_key and base_url == legacy_url:
                api_key = legacy_api_key.strip()
            configured_models = (
                ninfer_models if provider_id == "ninfer" else ()
            )
            configuration_error = (
                ninfer_configuration_error if provider_id == "ninfer" else ""
            )
            managed_default = bool(
                provider_id == "ninfer"
                and configured_models
                and any(model.start_command for model in configured_models)
            )
            providers.append(
                ProviderConfig(
                    provider_id=provider_id,
                    display_name=display_name,
                    base_url=base_url,
                    api_key=api_key,
                    enabled=_env_enabled(enabled_env),
                    managed=(
                        _env_enabled(managed_env, managed_default)
                        if managed_env
                        else False
                    ),
                    configured_models=configured_models,
                    configuration_error=configuration_error,
                    manager_mode=bool(
                        provider_id == "ninfer"
                        and _env_enabled(
                            "NINFER_MANAGER_ENABLED",
                            False,
                        )
                    ),
                )
            )
        if legacy_url and all(provider.base_url != legacy_url for provider in providers):
            providers.append(
                ProviderConfig(
                    provider_id="custom",
                    display_name="Custom OpenAI",
                    base_url=legacy_url,
                    api_key=legacy_api_key.strip(),
                )
            )
        return cls(
            providers,
            timeout_sec=timeout_sec,
            runtime_timeout_sec=runtime_timeout_sec,
            request_get=request_get,
            request_post=request_post,
            process_popen=process_popen,
            process_run=process_run,
            sleep=sleep,
        )

    @property
    def providers(self) -> tuple[ProviderConfig, ...]:
        return self._providers

    def get_provider(self, provider_id: str) -> ProviderConfig | None:
        return self._provider_by_id.get(provider_id.strip().lower())

    def canonical_model_id(self, provider_id: str, model_id: str) -> str:
        return _canonical_model_id(provider_id.strip().lower(), model_id)

    def matching_model(
        self,
        provider_id: str,
        model_id: str,
        models: Iterable[CatalogModel],
    ) -> CatalogModel | None:
        canonical = self.canonical_model_id(provider_id, model_id)
        return next(
            (
                model
                for model in models
                if self.canonical_model_id(provider_id, model.model_id)
                == canonical
            ),
            None,
        )

    def runtime_state(
        self,
        provider_id: str,
        model_id: str,
    ) -> tuple[str, str] | None:
        key = self._runtime_key(provider_id, model_id)
        with self._runtime_lock:
            override = self._runtime_overrides.get(key)
        if override is None:
            return None
        return override.state, override.detail

    def runtime_capability(self, provider_id: str) -> tuple[bool, str]:
        provider = self.get_provider(provider_id)
        if provider is None:
            return False, f"Unknown model provider: {provider_id}"
        with self._runtime_lock:
            supported = self._runtime_support.get(provider.provider_id, False)
        if supported:
            return True, f"{provider.display_name} lifecycle API is available"
        if not provider.managed:
            return (
                False,
                f"{provider.display_name} lifecycle is managed by its own application",
            )
        return (
            False,
            f"{provider.display_name} does not expose a supported lifecycle API",
        )

    def _configured_model(
        self,
        provider: ProviderConfig,
        model_id: str,
    ) -> ConfiguredModel | None:
        canonical = self.canonical_model_id(provider.provider_id, model_id)
        return next(
            (
                model
                for model in provider.configured_models
                if self.canonical_model_id(provider.provider_id, model.model_id)
                == canonical
            ),
            None,
        )

    def _model_runtime_supported(
        self,
        provider: ProviderConfig,
        model_id: str = "",
    ) -> bool:
        with self._runtime_lock:
            supported = self._runtime_support.get(provider.provider_id, False)
        if not supported:
            return False
        if provider.lifecycle_kind != "ninfer_process":
            return True
        if not model_id:
            return any(model.start_command for model in provider.configured_models)
        configured = self._configured_model(provider, model_id)
        return configured is not None and bool(configured.start_command)

    def available_actions(
        self,
        provider_id: str,
        load_state: str,
        model_id: str = "",
    ) -> tuple[str, ...]:
        provider = self.get_provider(provider_id)
        if (
            provider is None
            or not provider.runtime_commands
            or not self._model_runtime_supported(provider, model_id)
        ):
            return ()
        state = load_state.strip().lower()
        if state in _TRANSITIONAL_STATES:
            return ()
        if state == "loaded":
            actions = ["unload"]
            if "sleep" in provider.runtime_commands:
                actions.insert(0, "sleep")
            return tuple(actions)
        if state == "sleeping":
            return tuple(
                command
                for command in ("wake", "unload")
                if command in provider.runtime_commands
            )
        if state == "error":
            return tuple(
                command
                for command in ("load", "unload")
                if command in provider.runtime_commands
            )
        return ("load",) if "load" in provider.runtime_commands else ()

    def resolve(
        self,
        provider_id: str,
        *,
        fallback_base_url: str,
        fallback_api_key: str,
    ) -> ProviderConfig:
        requested = provider_id.strip().lower()
        if requested and requested != "auto":
            provider = self.get_provider(requested)
            if provider is None:
                raise ValueError(f"Unknown model provider: {provider_id}")
            return provider
        fallback_url = _normalized_url(fallback_base_url)
        for provider in self._providers:
            if provider.base_url == fallback_url:
                return provider
        return ProviderConfig(
            provider_id="custom",
            display_name="Custom OpenAI",
            base_url=fallback_url,
            api_key=fallback_api_key.strip(),
        )

    def probe(self, provider_id: str) -> ProviderProbe:
        provider = self.get_provider(provider_id)
        if provider is None:
            raise ValueError(f"Unknown model provider: {provider_id}")
        return self._probe_provider(provider)

    def probe_all(self) -> tuple[ProviderProbe, ...]:
        if not self._providers:
            return ()
        results: dict[str, ProviderProbe] = {}
        with ThreadPoolExecutor(
            max_workers=len(self._providers),
            thread_name_prefix="model-provider",
        ) as executor:
            futures = {
                executor.submit(self._probe_provider, provider): provider.provider_id
                for provider in self._providers
            }
            for future in as_completed(futures):
                provider_id = futures[future]
                try:
                    results[provider_id] = future.result()
                except Exception as exc:
                    provider = self._provider_by_id[provider_id]
                    results[provider_id] = ProviderProbe(
                        provider=provider,
                        reachable=False,
                        status="error",
                        detail=str(exc),
                        latency_sec=0.0,
                    )
        return tuple(results[provider.provider_id] for provider in self._providers)

    def control_runtime(
        self,
        provider_id: str,
        model_id: str,
        command: str,
    ) -> RuntimeControlResult:
        provider = self.get_provider(provider_id)
        normalized_command = command.strip().lower()
        if provider is None:
            return RuntimeControlResult(
                False,
                provider_id,
                model_id,
                "error",
                f"Unknown model provider: {provider_id}",
            )
        if not provider.runtime_commands:
            return RuntimeControlResult(
                False,
                provider.provider_id,
                model_id,
                "unsupported",
                f"{provider.display_name} lifecycle is managed by its own application",
            )
        if not self._model_runtime_supported(provider, model_id):
            _, capability_detail = self.runtime_capability(provider.provider_id)
            if provider.lifecycle_kind == "ninfer_process":
                configured = self._configured_model(provider, model_id)
                if configured is None:
                    capability_detail = (
                        f"{model_id} is not in the configured NInfer artifact catalog"
                    )
                elif not configured.start_command:
                    capability_detail = (
                        f"{model_id} has no configured NInfer start command"
                    )
            return RuntimeControlResult(
                False,
                provider.provider_id,
                model_id,
                "unsupported",
                capability_detail,
            )
        if normalized_command not in provider.runtime_commands:
            return RuntimeControlResult(
                False,
                provider.provider_id,
                model_id,
                "unsupported",
                f"{provider.display_name} does not support {normalized_command}",
            )

        if provider.lifecycle_kind not in {
            "vllm_manager",
            "ninfer_manager",
        }:
            return self._start_native_runtime_operation(
                provider,
                model_id,
                normalized_command,
            )
        return self._control_vllm_manager(
            provider,
            model_id,
            normalized_command,
        )

    def _control_vllm_manager(
        self,
        provider: ProviderConfig,
        model_id: str,
        command: str,
    ) -> RuntimeControlResult:
        headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}
        try:
            response = self._request_post(
                f"{provider.manager_url}/{command}",
                json={"model_id": model_id},
                headers=headers,
                timeout=self._timeout_sec,
            )
            status_code = int(getattr(response, "status_code", 200))
            payload = response.json()
            if status_code in {401, 403}:
                return RuntimeControlResult(
                    False,
                    provider.provider_id,
                    model_id,
                    "auth_error",
                    f"Authentication rejected with HTTP {status_code}",
                )
            if status_code >= 400:
                detail = (
                    str(payload.get("detail", "")).strip()
                    if isinstance(payload, dict)
                    else ""
                )
                return RuntimeControlResult(
                    False,
                    provider.provider_id,
                    model_id,
                    "error",
                    detail or f"Runtime command failed with HTTP {status_code}",
                )
            state = (
                str(payload.get("state", "")).strip()
                if isinstance(payload, dict)
                else ""
            ) or "unknown"
            detail = (
                str(payload.get("detail", "")).strip()
                if isinstance(payload, dict)
                else ""
            )
            return RuntimeControlResult(
                True,
                provider.provider_id,
                model_id,
                state,
                detail or f"{command} accepted",
            )
        except requests.RequestException as exc:
            return RuntimeControlResult(
                False,
                provider.provider_id,
                model_id,
                "offline",
                str(exc),
            )
        except (TypeError, ValueError) as exc:
            return RuntimeControlResult(
                False,
                provider.provider_id,
                model_id,
                "invalid_response",
                str(exc),
            )

    def _start_native_runtime_operation(
        self,
        provider: ProviderConfig,
        model_id: str,
        command: str,
    ) -> RuntimeControlResult:
        key = self._runtime_key(provider.provider_id, model_id)
        transitional_state = "loading" if command == "load" else "unloading"
        with self._runtime_lock:
            current = self._runtime_overrides.get(key)
            thread = self._runtime_threads.get(key)
            if (
                current is not None
                and current.state in _TRANSITIONAL_STATES
                and thread is not None
                and thread.is_alive()
            ):
                return RuntimeControlResult(
                    True,
                    provider.provider_id,
                    model_id,
                    current.state,
                    current.detail,
                )
            detail = (
                f"{provider.display_name} is loading {model_id}"
                if command == "load"
                else f"{provider.display_name} is unloading {model_id}"
            )
            self._runtime_overrides[key] = _RuntimeOverride(
                transitional_state,
                detail,
                time.monotonic(),
            )
            thread = threading.Thread(
                target=self._run_native_runtime_operation,
                args=(provider, model_id, command),
                name=f"{provider.provider_id}-{command}",
                daemon=True,
            )
            self._runtime_threads[key] = thread
            thread.start()
        return RuntimeControlResult(
            True,
            provider.provider_id,
            model_id,
            transitional_state,
            detail,
        )

    def _run_native_runtime_operation(
        self,
        provider: ProviderConfig,
        model_id: str,
        command: str,
    ) -> None:
        key = self._runtime_key(provider.provider_id, model_id)
        try:
            if provider.lifecycle_kind == "lmstudio_native":
                detail = self._control_lmstudio(provider, model_id, command)
            elif provider.lifecycle_kind == "unsloth_native":
                detail = self._control_unsloth(provider, model_id, command)
            elif provider.lifecycle_kind == "ninfer_process":
                detail = self._control_ninfer(provider, model_id, command)
            else:
                raise ValueError(
                    f"Unsupported lifecycle adapter: {provider.lifecycle_kind}"
                )
            state = "loaded" if command == "load" else "unloaded"
            override = _RuntimeOverride(state, detail, time.monotonic())
        except _LifecycleUnsupported as exc:
            with self._runtime_lock:
                self._runtime_support[provider.provider_id] = False
            override = _RuntimeOverride(
                "error",
                str(exc),
                time.monotonic(),
            )
        except Exception as exc:
            message = str(exc)
            if provider.api_key:
                message = message.replace(provider.api_key, "[redacted]")
            override = _RuntimeOverride(
                "error",
                message or f"{provider.display_name} runtime command failed",
                time.monotonic(),
            )
        with self._runtime_lock:
            self._runtime_overrides[key] = override
            self._runtime_threads.pop(key, None)

    def _control_lmstudio(
        self,
        provider: ProviderConfig,
        model_id: str,
        command: str,
    ) -> str:
        root_url = self._provider_root_url(provider)
        headers = self._provider_headers(provider)
        if command == "load":
            response = self._request_post(
                f"{root_url}/api/v1/models/load",
                json={"model": model_id},
                headers=headers,
                timeout=self._runtime_timeout_sec,
            )
            payload = self._checked_payload(response)
            instance_id = _first_text(payload, "instance_id")
            return (
                f"LM Studio loaded {model_id}"
                + (f" as {instance_id}" if instance_id else "")
            )

        response = self._request_get(
            f"{root_url}/api/v1/models",
            headers=headers,
            timeout=self._timeout_sec,
        )
        payload = self._checked_payload(response)
        instance_ids: list[str] = []
        for raw_row in _rows_from_payload(payload):
            if not isinstance(raw_row, dict):
                continue
            if _first_text(raw_row, "key", "id") != model_id:
                continue
            for instance in raw_row.get("loaded_instances") or []:
                if isinstance(instance, dict):
                    instance_id = _first_text(instance, "id", "instance_id")
                    if instance_id:
                        instance_ids.append(instance_id)
        if not instance_ids:
            return f"LM Studio model {model_id} is already unloaded"
        for instance_id in instance_ids:
            unload_response = self._request_post(
                f"{root_url}/api/v1/models/unload",
                json={"instance_id": instance_id},
                headers=headers,
                timeout=self._runtime_timeout_sec,
            )
            self._checked_payload(unload_response)
        return (
            f"LM Studio unloaded {len(instance_ids)} instance(s) of {model_id}"
        )

    def _control_unsloth(
        self,
        provider: ProviderConfig,
        model_id: str,
        command: str,
    ) -> str:
        root_url = self._provider_root_url(provider)
        headers = self._provider_headers(provider)
        model_path, variant = _unsloth_model_parts(model_id)
        payload: dict[str, Any] = {"model_path": model_path}
        if command == "load" and variant:
            payload["gguf_variant"] = variant
        response = self._request_post(
            f"{root_url}/v1/{command}",
            json=payload,
            headers=headers,
            timeout=self._runtime_timeout_sec,
        )
        result = self._checked_payload(response)
        status = _first_text(result, "status") or (
            "loaded" if command == "load" else "unloaded"
        )
        return f"Unsloth Studio {status} {model_id}"

    def _control_ninfer(
        self,
        provider: ProviderConfig,
        model_id: str,
        command: str,
    ) -> str:
        configured = self._configured_model(provider, model_id)
        if configured is None:
            raise ValueError(
                f"{model_id} is not in the configured NInfer artifact catalog"
            )
        artifact_path = configured.artifact_path
        if not artifact_path:
            raise ValueError(f"NInfer artifact path is not configured for {model_id}")
        if not Path(artifact_path).is_file():
            raise FileNotFoundError(f"NInfer artifact is missing: {artifact_path}")

        key = self._runtime_key(provider.provider_id, model_id)
        if command == "load":
            if self._ninfer_model_is_loaded(provider, model_id):
                return f"NInfer model {model_id} is already loaded"
            if not configured.start_command:
                raise ValueError(
                    f"NInfer model {model_id} has no configured start command"
                )
            environment = os.environ.copy()
            environment.update(dict(configured.environment))
            environment.setdefault("NINFER_MODEL_ID", model_id)
            environment.setdefault("NINFER_MODEL_ARTIFACT", artifact_path)
            process = self._process_popen(
                self._expanded_command(
                    configured.start_command,
                    provider,
                    configured,
                ),
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            with self._runtime_lock:
                self._runtime_processes[key] = process
            self._wait_for_ninfer_state(
                provider,
                model_id,
                loaded=True,
                process=process,
            )
            return f"NInfer loaded {model_id}"

        if configured.stop_command:
            completed = self._process_run(
                self._expanded_command(
                    configured.stop_command,
                    provider,
                    configured,
                ),
                env={**os.environ, **dict(configured.environment)},
                capture_output=True,
                text=True,
                timeout=self._runtime_timeout_sec,
                check=False,
            )
            if int(getattr(completed, "returncode", 0)) != 0:
                stderr = str(getattr(completed, "stderr", "")).strip()
                raise RuntimeError(
                    stderr
                    or f"NInfer stop command exited with {completed.returncode}"
                )
        else:
            with self._runtime_lock:
                process = self._runtime_processes.get(key)
            if process is None:
                if self._ninfer_model_is_loaded(provider, model_id):
                    raise RuntimeError(
                        "NInfer model is loaded by an external process; configure "
                        "stop_command to unload it safely"
                    )
                return f"NInfer model {model_id} is already unloaded"
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=min(10.0, self._runtime_timeout_sec))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=min(5.0, self._runtime_timeout_sec))

        self._wait_for_ninfer_state(provider, model_id, loaded=False)
        with self._runtime_lock:
            self._runtime_processes.pop(key, None)
        return f"NInfer unloaded {model_id}"

    @staticmethod
    def _expanded_command(
        command: tuple[str, ...],
        provider: ProviderConfig,
        model: ConfiguredModel,
    ) -> list[str]:
        replacements = {
            "{artifact}": model.artifact_path,
            "{model_id}": model.model_id,
            "{base_url}": provider.base_url,
        }
        expanded: list[str] = []
        for part in command:
            value = part
            for token, replacement in replacements.items():
                value = value.replace(token, replacement)
            expanded.append(value)
        return expanded

    def _ninfer_model_is_loaded(
        self,
        provider: ProviderConfig,
        model_id: str,
    ) -> bool:
        try:
            response = self._request_get(
                provider.models_url,
                headers=self._provider_headers(provider),
                timeout=self._timeout_sec,
            )
            if int(getattr(response, "status_code", 200)) >= 400:
                return False
            response.raise_for_status()
            payload = response.json()
            return any(
                isinstance(row, dict)
                and _first_text(row, "id", "key", "model", "name") == model_id
                for row in _rows_from_payload(payload)
            )
        except (requests.RequestException, TypeError, ValueError):
            return False

    def _wait_for_ninfer_state(
        self,
        provider: ProviderConfig,
        model_id: str,
        *,
        loaded: bool,
        process: Any | None = None,
    ) -> None:
        deadline = time.monotonic() + self._runtime_timeout_sec
        while time.monotonic() < deadline:
            if self._ninfer_model_is_loaded(provider, model_id) is loaded:
                return
            if process is not None:
                return_code = process.poll()
                if return_code not in {None, 0}:
                    raise RuntimeError(
                        f"NInfer start command exited with {return_code}"
                    )
            self._sleep(0.1)
        expected = "ready" if loaded else "stopped"
        raise TimeoutError(
            f"NInfer model {model_id} did not become {expected} within "
            f"{self._runtime_timeout_sec:.1f}s"
        )

    @staticmethod
    def _provider_root_url(provider: ProviderConfig) -> str:
        base_url = provider.base_url.rstrip("/")
        return base_url[:-3] if base_url.endswith("/v1") else base_url

    @staticmethod
    def _provider_headers(provider: ProviderConfig) -> dict[str, str]:
        if not provider.api_key:
            return {}
        return {"Authorization": f"Bearer {provider.api_key}"}

    @staticmethod
    def _checked_payload(response: Any) -> dict[str, Any]:
        status_code = int(getattr(response, "status_code", 200))
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ValueError("Provider returned invalid JSON") from exc
        if status_code >= 400:
            detail = ""
            if isinstance(payload, dict):
                raw_detail = payload.get("detail")
                if isinstance(raw_detail, str):
                    detail = raw_detail
                error = payload.get("error")
                if not detail and isinstance(error, dict):
                    detail = str(error.get("message", "")).strip()
            message = (
                detail or f"Provider runtime command failed with HTTP {status_code}"
            )
            if status_code in {404, 405, 501}:
                raise _LifecycleUnsupported(
                    f"Provider does not expose a supported lifecycle API: {message}"
                )
            raise RuntimeError(message)
        response.raise_for_status()
        if not isinstance(payload, dict):
            raise ValueError("Provider returned a non-object JSON response")
        return payload

    def ensure_runtime_ready(
        self,
        provider_id: str,
        model: CatalogModel,
        *,
        requested_model_id: str = "",
    ) -> RuntimeControlResult:
        target_model_id = requested_model_id or model.model_id
        if not model.installed:
            return RuntimeControlResult(
                False,
                provider_id,
                target_model_id,
                "error",
                model.detail or f"{target_model_id} is not installed",
            )
        if not model.available:
            return RuntimeControlResult(
                False,
                provider_id,
                target_model_id,
                "unavailable",
                model.detail or f"{target_model_id} is not available",
            )
        if model.load_state == "sleeping":
            return self.control_runtime(provider_id, target_model_id, "wake")
        if model.load_state in {"unloaded", "error", "configured", "unknown"}:
            return self.control_runtime(provider_id, target_model_id, "load")
        return RuntimeControlResult(
            True,
            provider_id,
            target_model_id,
            model.load_state,
            (
                "Model is ready"
                if model.load_state == "loaded"
                else f"Model runtime is already {model.load_state}"
            ),
        )

    def _runtime_key(
        self,
        provider_id: str,
        model_id: str,
    ) -> tuple[str, str]:
        normalized_provider = provider_id.strip().lower()
        return (
            normalized_provider,
            _canonical_model_id(normalized_provider, model_id),
        )

    def _configured_catalog(
        self,
        provider: ProviderConfig,
    ) -> tuple[CatalogModel, ...]:
        models: list[CatalogModel] = []
        for configured in provider.configured_models:
            artifact_required = provider.lifecycle_kind == "ninfer_process"
            installed = bool(
                not configured.artifact_path
                or Path(configured.artifact_path).is_file()
            )
            if artifact_required and not configured.artifact_path:
                installed = False
            runtime_managed = self._model_runtime_supported(
                provider,
                configured.model_id,
            )
            available = installed and (
                runtime_managed or provider.lifecycle_kind == "external"
            )
            load_state = "unloaded" if installed else "error"
            detail = configured.detail
            if not installed:
                detail = (
                    f"Configured artifact is missing: {configured.artifact_path}"
                    if configured.artifact_path
                    else "Configured artifact path is missing"
                )
            elif not runtime_managed and provider.managed:
                detail = detail or (
                    f"{provider.display_name} lifecycle control is unavailable"
                )
            elif configured.artifact_path:
                detail = detail or f"Installed artifact: {configured.artifact_path}"
            models.append(
                CatalogModel(
                    provider_id=provider.provider_id,
                    provider_name=provider.display_name,
                    model_id=configured.model_id,
                    display_name=configured.display_name or configured.model_id,
                    capability=configured.capability,
                    load_state=load_state,
                    selectable=configured.selectable and available,
                    detail=detail,
                    runtime_managed=runtime_managed,
                    available_actions=(
                        self.available_actions(
                            provider.provider_id,
                            load_state,
                            configured.model_id,
                        )
                        if installed
                        else ()
                    ),
                    installed=installed,
                    available=available,
                )
            )
        return tuple(models)

    def _merge_configured_models(
        self,
        provider: ProviderConfig,
        discovered: tuple[CatalogModel, ...],
    ) -> tuple[CatalogModel, ...]:
        configured = self._configured_catalog(provider)
        if not configured:
            return discovered
        discovered_by_id = {
            self.canonical_model_id(provider.provider_id, model.model_id): model
            for model in discovered
        }
        merged: list[CatalogModel] = []
        configured_ids: set[str] = set()
        for local_model in configured:
            canonical = self.canonical_model_id(
                provider.provider_id,
                local_model.model_id,
            )
            configured_ids.add(canonical)
            live_model = discovered_by_id.get(canonical)
            if live_model is None:
                merged.append(local_model)
                continue
            runtime_managed = self._model_runtime_supported(
                provider,
                local_model.model_id,
            )
            merged.append(
                replace(
                    live_model,
                    display_name=(
                        local_model.display_name or live_model.display_name
                    ),
                    capability=(
                        local_model.capability
                        if local_model.capability != "unknown"
                        else live_model.capability
                    ),
                    selectable=live_model.selectable,
                    detail=live_model.detail or local_model.detail,
                    runtime_managed=runtime_managed,
                    available_actions=self.available_actions(
                        provider.provider_id,
                        live_model.load_state,
                        local_model.model_id,
                    ),
                    installed=local_model.installed,
                    available=True,
                )
            )
        merged.extend(
            model
            for model in discovered
            if self.canonical_model_id(provider.provider_id, model.model_id)
            not in configured_ids
        )
        return tuple(merged)

    def _apply_runtime_overrides(
        self,
        provider: ProviderConfig,
        models: tuple[CatalogModel, ...],
    ) -> tuple[CatalogModel, ...]:
        now = time.monotonic()
        decorated: list[CatalogModel] = []
        with self._runtime_lock:
            for model in models:
                key = self._runtime_key(provider.provider_id, model.model_id)
                override = self._runtime_overrides.get(key)
                load_state = model.load_state
                detail = model.detail
                if override is not None:
                    age_sec = now - override.updated_at
                    source_has_terminal_state = (
                        override.state in {"loaded", "unloaded"}
                        and load_state == override.state
                    )
                    expired = (
                        override.state not in _TRANSITIONAL_STATES
                        and age_sec > 60.0
                    )
                    if source_has_terminal_state or expired:
                        self._runtime_overrides.pop(key, None)
                    else:
                        load_state = override.state
                        detail = override.detail
                decorated.append(
                    replace(
                        model,
                        load_state=load_state,
                        detail=detail,
                        runtime_managed=self._model_runtime_supported(
                            provider,
                            model.model_id,
                        ),
                        available_actions=(
                            self.available_actions(
                                provider.provider_id,
                                load_state,
                                model.model_id,
                            )
                            if model.installed and model.available
                            else ()
                        ),
                    )
                )
        return tuple(decorated)

    def _set_runtime_support(
        self,
        provider_id: str,
        supported: bool,
    ) -> None:
        with self._runtime_lock:
            self._runtime_support[provider_id] = supported

    def _probe_provider(self, provider: ProviderConfig) -> ProviderProbe:
        started = time.monotonic()
        headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}
        last_status = "offline"
        last_detail = ""
        for models_url in provider.models_urls:
            try:
                response = self._request_get(
                    models_url,
                    headers=headers,
                    timeout=self._timeout_sec,
                )
                latency_sec = time.monotonic() - started
                status_code = int(getattr(response, "status_code", 200))
                if status_code in {401, 403}:
                    configured_models = self._apply_runtime_overrides(
                        provider,
                        self._configured_catalog(provider),
                    )
                    return ProviderProbe(
                        provider=provider,
                        reachable=False,
                        status="auth_error",
                        detail=f"Authentication rejected with HTTP {status_code}",
                        latency_sec=latency_sec,
                        models=configured_models,
                    )
                if status_code in {404, 405} and models_url != provider.models_url:
                    last_status = "unsupported_native_catalog"
                    last_detail = f"HTTP {status_code} from {models_url}"
                    if provider.provider_id == "lmstudio":
                        self._set_runtime_support(provider.provider_id, False)
                    continue
                response.raise_for_status()
                payload = response.json()
                models = self._parse_models(provider, payload)
                if provider.provider_id == "lmstudio":
                    native_catalog = "/api/v1/models" in models_url
                    self._set_runtime_support(
                        provider.provider_id,
                        provider.managed and native_catalog,
                    )
                models = self._merge_configured_models(provider, models)
                models = self._apply_runtime_overrides(provider, models)
                loaded_count = sum(
                    1 for model in models if model.load_state == "loaded"
                )
                return ProviderProbe(
                    provider=provider,
                    reachable=True,
                    status="online",
                    detail=(
                        f"{len(models)} model(s); {loaded_count} loaded"
                    ),
                    latency_sec=latency_sec,
                    models=models,
                )
            except requests.Timeout:
                last_status = "timeout"
                last_detail = f"No response within {self._timeout_sec:.1f}s"
            except requests.RequestException as exc:
                last_status = "offline"
                last_detail = str(exc)
            except (TypeError, ValueError) as exc:
                last_status = "invalid_response"
                last_detail = str(exc)
            if models_url == provider.models_url:
                break
        configured_models = self._apply_runtime_overrides(
            provider,
            self._configured_catalog(provider),
        )
        if provider.configuration_error:
            last_status = "configuration_error"
            last_detail = provider.configuration_error
        elif configured_models:
            last_detail = (
                f"{last_detail}; " if last_detail else ""
            ) + f"{len(configured_models)} configured model(s) available offline"
        return ProviderProbe(
            provider=provider,
            reachable=False,
            status=last_status,
            detail=last_detail,
            latency_sec=time.monotonic() - started,
            models=configured_models,
        )

    @staticmethod
    def _parse_models(
        provider: ProviderConfig,
        payload: Any,
    ) -> tuple[CatalogModel, ...]:
        models: list[CatalogModel] = []
        seen: set[str] = set()
        for raw_row in _rows_from_payload(payload):
            if isinstance(raw_row, str):
                row: dict[str, Any] = {"id": raw_row}
            elif isinstance(raw_row, dict):
                row = raw_row
            else:
                continue
            model_id = _first_text(row, "id", "key", "model", "name")
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            display_name = _first_text(row, "display_name", "name") or model_id
            owned_by = _first_text(row, "owned_by")
            detail = _first_text(row, "detail")
            load_state = _load_state(row)
            installed = _bool_value(row.get("installed"), True)
            available = _bool_value(
                row.get("available"),
                _bool_value(row.get("selectable"), True),
            )
            # NInfer advertises only its already-resident model alias. Unlike a
            # catalog/manager endpoint, a successful /v1/models response means
            # the model is loaded and ready to serve.
            if provider.provider_id == "ninfer" and load_state == "unknown":
                load_state = "loaded"
            models.append(
                CatalogModel(
                    provider_id=provider.provider_id,
                    provider_name=provider.display_name,
                    model_id=model_id,
                    display_name=display_name,
                    capability=_capability(row, model_id),
                    load_state=load_state,
                    selectable=_bool_value(
                        row.get("selectable"),
                        available,
                    ),
                    detail=detail or (f"owned by {owned_by}" if owned_by else ""),
                    runtime_managed=bool(provider.runtime_commands),
                    installed=installed,
                    available=available,
                )
            )
        return tuple(models)
