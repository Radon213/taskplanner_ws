"""Always-on multi-model lifecycle manager and OpenAI-compatible vLLM proxy."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import threading
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx
from pydantic import BaseModel
import requests
import uvicorn


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


DEFAULT_EXTRA_ARGS = (
    "--enable-prefix-caching",
    "--enable-chunked-prefill",
    "--max-num-batched-tokens",
    "8192",
    "--limit-mm-per-prompt",
    '{"image":3}',
    "--no-enable-log-requests",
)


def _string_tuple(value: Any, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        return tuple(shlex.split(value))
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ValueError(f"Expected a string or list, got {type(value).__name__}")


@dataclass(frozen=True)
class ModelProfile:
    source_model_id: str
    served_model_name: str
    display_name: str
    capabilities: tuple[str, ...]
    max_model_len: int
    max_num_seqs: int
    gpu_memory_utilization: float
    generation_config: str
    reasoning_parser: str
    extra_args: tuple[str, ...]
    local_only: bool = True
    client_model_id: str = ""

    @property
    def advertised_model_id(self) -> str:
        return self.client_model_id or self.served_model_name

    @classmethod
    def from_payload(cls, row: dict[str, Any]) -> "ModelProfile":
        source_model_id = str(
            row.get("source_model_id", row.get("model_id", ""))
        ).strip()
        if not source_model_id:
            raise ValueError("Every vLLM catalog entry needs source_model_id")
        served_name = str(
            row.get("served_model_name", source_model_id)
        ).strip()
        if not served_name:
            served_name = source_model_id
        capabilities = _string_tuple(
            row.get("capabilities"),
            default=("text", "image"),
        )
        client_model_id = str(
            row.get("client_model_id", served_name)
        ).strip() or served_name
        return cls(
            source_model_id=source_model_id,
            served_model_name=served_name,
            client_model_id=client_model_id,
            display_name=str(row.get("display_name", served_name)).strip()
            or served_name,
            capabilities=capabilities or ("text",),
            max_model_len=max(1, int(row.get("max_model_len", 8192))),
            max_num_seqs=max(1, int(row.get("max_num_seqs", 1))),
            gpu_memory_utilization=min(
                1.0,
                max(0.05, float(row.get("gpu_memory_utilization", 0.80))),
            ),
            generation_config=str(
                row.get("generation_config", "vllm")
            ).strip(),
            reasoning_parser=str(row.get("reasoning_parser", "")).strip(),
            extra_args=_string_tuple(
                row.get("extra_args"),
                default=DEFAULT_EXTRA_ARGS,
            ),
            local_only=bool(row.get("local_only", True)),
        )

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    self.advertised_model_id,
                    self.served_model_name,
                    self.source_model_id,
                )
            )
        )


@dataclass(frozen=True)
class Settings:
    bind_host: str
    manager_port: int
    worker_host: str
    worker_port: int
    profiles: tuple[ModelProfile, ...]
    default_model_id: str
    api_key: str
    startup_timeout_sec: float
    shutdown_timeout_sec: float
    request_timeout_sec: float
    sleep_enabled: bool
    auto_start: bool
    log_path: Path
    catalog_path: Path
    hf_cache_root: Path

    @staticmethod
    def _legacy_profile(model_id: str) -> ModelProfile:
        served_name = os.environ.get(
            "VLLM_MANAGER_SERVED_MODEL_NAME",
            model_id,
        ).strip()
        extra_args = os.environ.get("VLLM_MANAGER_EXTRA_ARGS", "").strip()
        return ModelProfile(
            source_model_id=model_id,
            served_model_name=served_name or model_id,
            display_name=served_name or model_id,
            capabilities=("text", "image"),
            max_model_len=int(
                os.environ.get("VLLM_MANAGER_MAX_MODEL_LEN", "8192")
            ),
            max_num_seqs=int(
                os.environ.get("VLLM_MANAGER_MAX_NUM_SEQS", "1")
            ),
            gpu_memory_utilization=float(
                os.environ.get(
                    "VLLM_MANAGER_GPU_MEMORY_UTILIZATION",
                    "0.80",
                )
            ),
            generation_config=os.environ.get(
                "VLLM_MANAGER_GENERATION_CONFIG",
                "vllm",
            ).strip(),
            reasoning_parser=os.environ.get(
                "VLLM_MANAGER_REASONING_PARSER",
                "gemma4",
            ).strip(),
            extra_args=(
                tuple(shlex.split(extra_args))
                if extra_args
                else DEFAULT_EXTRA_ARGS
            ),
            local_only=_env_bool("VLLM_MANAGER_LOCAL_ONLY", True),
        )

    @classmethod
    def _load_profiles(
        cls,
        catalog_path: Path,
    ) -> tuple[tuple[ModelProfile, ...], str]:
        if not catalog_path.is_file():
            return (), ""
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        rows = payload.get("models", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise ValueError("vLLM model catalog 'models' must be a list")
        profiles = tuple(
            ModelProfile.from_payload(row)
            for row in rows
            if isinstance(row, dict) and bool(row.get("enabled", True))
        )
        aliases: set[str] = set()
        for profile in profiles:
            for alias in profile.aliases:
                if alias in aliases:
                    raise ValueError(
                        f"Duplicate vLLM model alias in catalog: {alias}"
                    )
                aliases.add(alias)
        default_model_id = (
            str(payload.get("default_model_id", "")).strip()
            if isinstance(payload, dict)
            else ""
        )
        return profiles, default_model_id

    @classmethod
    def from_environment(cls) -> "Settings":
        catalog_path = Path(
            os.environ.get(
                "VLLM_MANAGER_CATALOG_PATH",
                "/opt/taskplanner-vllm-manager/models.json",
            )
        )
        profiles, catalog_default = cls._load_profiles(catalog_path)
        requested_default = os.environ.get(
            "VLLM_MANAGER_MODEL_ID",
            catalog_default or "unsloth/gemma-4-E4B-it-NVFP4",
        ).strip()
        known_aliases = {
            alias
            for profile in profiles
            for alias in profile.aliases
        }
        if not profiles or requested_default not in known_aliases:
            legacy = cls._legacy_profile(requested_default)
            profiles = (*profiles, legacy)
        if not profiles:
            raise ValueError("The vLLM manager has no configured models")
        return cls(
            bind_host=os.environ.get("VLLM_MANAGER_BIND_HOST", "127.0.0.1").strip(),
            manager_port=int(os.environ.get("VLLM_MANAGER_PORT", "8001")),
            worker_host=os.environ.get("VLLM_MANAGER_WORKER_HOST", "127.0.0.1").strip(),
            worker_port=int(os.environ.get("VLLM_MANAGER_WORKER_PORT", "8002")),
            profiles=tuple(profiles),
            default_model_id=requested_default or profiles[0].served_model_name,
            api_key=os.environ.get("VLLM_API_KEY", "").strip(),
            startup_timeout_sec=float(
                os.environ.get("VLLM_MANAGER_STARTUP_TIMEOUT_SEC", "600")
            ),
            shutdown_timeout_sec=float(
                os.environ.get("VLLM_MANAGER_SHUTDOWN_TIMEOUT_SEC", "45")
            ),
            request_timeout_sec=float(
                os.environ.get("VLLM_MANAGER_REQUEST_TIMEOUT_SEC", "600")
            ),
            sleep_enabled=_env_bool("VLLM_MANAGER_SLEEP_ENABLED", True),
            auto_start=_env_bool("VLLM_MANAGER_AUTO_START", False),
            log_path=Path(
                os.environ.get(
                    "VLLM_MANAGER_LOG_PATH",
                    "/tmp/taskplanner-vllm-worker.log",
                )
            ),
            catalog_path=catalog_path,
            hf_cache_root=Path(
                os.environ.get(
                    "VLLM_MANAGER_HF_CACHE_ROOT",
                    "/root/.cache/huggingface",
                )
            ),
        )


class ModelRequest(BaseModel):
    model_id: str = ""


class RuntimeManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._profiles_by_alias = {
            alias: profile
            for profile in settings.profiles
            for alias in profile.aliases
        }
        self._active_profile = self._resolve_profile(
            settings.default_model_id
        )
        self._transition_generation = 0
        self._state = "unloaded"
        self._detail = "vLLM manager is ready; model worker is stopped"
        self._last_error = ""
        self._updated_at = time.time()
        self._loaded_at = 0.0
        self._cached_models = {
            profile.served_model_name: self._is_cached(profile)
            for profile in settings.profiles
        }

    @property
    def worker_url(self) -> str:
        return f"http://{self.settings.worker_host}:{self.settings.worker_port}"

    def _headers(self) -> dict[str, str]:
        if not self.settings.api_key:
            return {}
        return {"Authorization": f"Bearer {self.settings.api_key}"}

    def _resolve_profile(self, model_id: str = "") -> ModelProfile:
        requested = model_id.strip() if model_id else ""
        if not requested:
            requested = self.settings.default_model_id
        profile = self._profiles_by_alias.get(requested)
        if profile is None:
            available = ", ".join(
                item.served_model_name for item in self.settings.profiles
            )
            raise ValueError(
                f"Unknown vLLM model {requested!r}; available models: {available}"
            )
        return profile

    def _is_cached(self, profile: ModelProfile) -> bool:
        source = Path(profile.source_model_id).expanduser()
        if source.is_absolute():
            return source.exists()
        repo_dir = (
            self.settings.hf_cache_root
            / "hub"
            / f"models--{profile.source_model_id.replace('/', '--')}"
        )
        snapshots = repo_dir / "snapshots"
        if not snapshots.is_dir():
            return False
        return any(snapshots.glob("*/config.json"))

    def _profile_is_active(self, profile: ModelProfile) -> bool:
        return (
            self._active_profile.served_model_name
            == profile.served_model_name
        )

    def _profile_selectable(self, profile: ModelProfile) -> bool:
        return (
            self._cached_models.get(profile.served_model_name, False)
            or not profile.local_only
        )

    def _set_state(
        self,
        state: str,
        detail: str,
        *,
        error: str = "",
        profile: ModelProfile | None = None,
        generation: int | None = None,
    ) -> bool:
        with self._lock:
            if (
                generation is not None
                and generation != self._transition_generation
            ):
                return False
            if profile is not None and not self._profile_is_active(profile):
                return False
            self._state = state
            self._detail = detail
            self._last_error = error
            self._updated_at = time.time()
            if state == "loaded":
                self._loaded_at = self._updated_at
            return True

    def snapshot(
        self,
        profile: ModelProfile | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            selected = profile or self._active_profile
            is_active = self._profile_is_active(selected)
            process = self._process
            pid = (
                process.pid
                if is_active
                and process is not None
                and process.poll() is None
                else 0
            )
            state = self._state if is_active else "unloaded"
            cached = self._cached_models.get(
                selected.served_model_name,
                False,
            )
            if is_active and not (
                state == "unloaded"
                and selected.local_only
                and not cached
            ):
                detail = self._detail
                last_error = self._last_error
                loaded_at = self._loaded_at
            elif cached:
                detail = "Model is cached locally and ready to load"
                last_error = ""
                loaded_at = 0.0
            elif selected.local_only:
                detail = "Model weights are not present in the local cache"
                last_error = ""
                loaded_at = 0.0
            else:
                detail = "Model will be downloaded when loading starts"
                last_error = ""
                loaded_at = 0.0
            return {
                "provider": "vllm",
                "model_id": selected.advertised_model_id,
                "source_model_id": selected.source_model_id,
                "display_name": selected.display_name,
                "state": state,
                "detail": detail,
                "last_error": last_error,
                "worker_pid": pid,
                "worker_url": self.worker_url,
                "sleep_enabled": self.settings.sleep_enabled,
                "active": is_active,
                "cached": cached,
                "updated_at": self._updated_at,
                "loaded_at": loaded_at,
            }

    def model_row(self, profile: ModelProfile) -> dict[str, Any]:
        snapshot = self.snapshot(profile)
        state = str(snapshot["state"])
        is_loaded = state == "loaded"
        selectable = self._profile_selectable(profile)
        return {
            "id": profile.advertised_model_id,
            "object": "model",
            "owned_by": "taskplanner-vllm-manager",
            "source_model_id": profile.source_model_id,
            "display_name": profile.display_name,
            "capabilities": list(profile.capabilities),
            "loaded": is_loaded,
            "loaded_instances": (
                [{"id": profile.advertised_model_id}]
                if is_loaded
                else []
            ),
            "load_state": state,
            "state": state,
            "status": state,
            "selectable": selectable,
            "active": bool(snapshot["active"]),
            "cached": bool(snapshot["cached"]),
            "detail": snapshot["detail"],
            "available_actions": self.available_actions(profile, state),
        }

    def model_rows(self) -> list[dict[str, Any]]:
        return [
            self.model_row(profile)
            for profile in self.settings.profiles
        ]

    def available_actions(
        self,
        profile: ModelProfile | None = None,
        state: str | None = None,
    ) -> list[str]:
        selected = profile or self._active_profile
        if not self._profile_selectable(selected):
            return []
        state = state or str(self.snapshot(selected)["state"])
        if state in {"unloaded", "error"}:
            return ["load"]
        if state == "loaded":
            actions = ["unload"]
            if self.settings.sleep_enabled:
                actions.insert(0, "sleep")
            return actions
        if state == "sleeping":
            return ["wake", "unload"]
        return []

    def _worker_command(self, profile: ModelProfile) -> list[str]:
        command = [
            "vllm",
            "serve",
            profile.source_model_id,
            "--served-model-name",
            profile.served_model_name,
            "--host",
            self.settings.worker_host,
            "--port",
            str(self.settings.worker_port),
            "--max-model-len",
            str(profile.max_model_len),
            "--max-num-seqs",
            str(profile.max_num_seqs),
            "--gpu-memory-utilization",
            str(profile.gpu_memory_utilization),
        ]
        if profile.generation_config:
            command.extend(
                ["--generation-config", profile.generation_config]
            )
        if profile.reasoning_parser:
            command.extend(
                ["--reasoning-parser", profile.reasoning_parser]
            )
        if self.settings.sleep_enabled:
            command.append("--enable-sleep-mode")
        command.extend(profile.extra_args)
        command.extend(
            [
                "--default-chat-template-kwargs",
                '{"enable_thinking": false}',
            ]
        )
        return command

    def request_load(self, model_id: str = "") -> dict[str, Any]:
        profile = self._resolve_profile(model_id)
        if not self._profile_selectable(profile):
            raise ValueError(
                f"{profile.display_name} is configured as local-only, "
                "but its weights are not in the Hugging Face cache"
            )
        with self._lock:
            same_profile = self._profile_is_active(profile)
            current_state = self._state
        if same_profile and current_state == "sleeping":
            return self.request_wake(profile.served_model_name)
        with self._lock:
            same_profile = self._profile_is_active(profile)
            if (
                same_profile
                and self._state in {"loaded", "loading", "waking"}
            ):
                return self.snapshot(profile)
            previous_name = self._active_profile.display_name
            previous_state = self._state
            self._transition_generation += 1
            generation = self._transition_generation
            self._active_profile = profile
            self._state = "loading"
            self._detail = (
                f"Switching from {previous_name} to {profile.display_name}"
                if previous_state not in {"unloaded", "error"}
                else f"Loading {profile.display_name}"
            )
            self._last_error = ""
            self._updated_at = time.time()
        threading.Thread(
            target=self._start_worker,
            args=(profile, generation),
            name="vllm-worker-start",
            daemon=True,
        ).start()
        return self.snapshot(profile)

    def _start_worker(
        self,
        profile: ModelProfile,
        generation: int,
    ) -> None:
        try:
            with self._lock:
                old_process = self._process
            if old_process is not None and old_process.poll() is None:
                self._terminate_process(old_process)

            with self._lock:
                if (
                    generation != self._transition_generation
                    or not self._profile_is_active(profile)
                ):
                    return
            self.settings.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = self.settings.log_path.open("wb", buffering=0)
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("VLLM_MANAGER_")
            }
            if self.settings.sleep_enabled:
                environment["VLLM_SERVER_DEV_MODE"] = "1"
            process = subprocess.Popen(
                self._worker_command(profile),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
            log_handle.close()
            with self._lock:
                if (
                    generation != self._transition_generation
                    or not self._profile_is_active(profile)
                ):
                    self._terminate_process(process)
                    return
                self._process = process

            deadline = time.monotonic() + self.settings.startup_timeout_sec
            while time.monotonic() < deadline:
                with self._lock:
                    if (
                        generation != self._transition_generation
                        or not self._profile_is_active(profile)
                    ):
                        self._terminate_process(process)
                        if self._process is process:
                            self._process = None
                        return
                if process.poll() is not None:
                    raise RuntimeError(
                        f"vLLM worker exited with code {process.returncode}: "
                        f"{self._tail_log()}"
                    )
                try:
                    response = requests.get(
                        f"{self.worker_url}/health",
                        headers=self._headers(),
                        timeout=2.0,
                    )
                    if response.ok:
                        updated = self._set_state(
                            "loaded",
                            f"{profile.display_name} is ready for inference",
                            profile=profile,
                            generation=generation,
                        )
                        if updated:
                            self._watch_worker(process)
                        return
                except requests.RequestException:
                    pass
                time.sleep(1.0)
            self._terminate_process(process)
            raise TimeoutError(
                f"vLLM worker did not become ready within "
                f"{self.settings.startup_timeout_sec:.0f}s"
            )
        except Exception as exc:
            self._set_state(
                "error",
                f"{profile.display_name} failed to start",
                error=self._sanitize(str(exc)),
                profile=profile,
                generation=generation,
            )

    def _watch_worker(self, process: subprocess.Popen[bytes]) -> None:
        return_code = process.wait()
        with self._lock:
            owns_process = self._process is process
            if owns_process:
                self._process = None
            intentional = (
                not owns_process
                or self._state in {"loading", "unloading", "unloaded"}
            )
        if not intentional:
            self._set_state(
                "error",
                "vLLM worker exited unexpectedly",
                error=self._sanitize(
                    f"exit code {return_code}: {self._tail_log()}"
                ),
            )

    def request_unload(self, model_id: str = "") -> dict[str, Any]:
        profile = self._resolve_profile(model_id)
        with self._lock:
            if not self._profile_is_active(profile):
                return self.snapshot(profile)
            if self._state == "unloaded":
                return self.snapshot(profile)
            if self._state == "unloading":
                return self.snapshot(profile)
            self._transition_generation += 1
            generation = self._transition_generation
            self._state = "unloading"
            self._detail = (
                f"Stopping {profile.display_name} and releasing model memory"
            )
            self._last_error = ""
            self._updated_at = time.time()
        threading.Thread(
            target=self._stop_worker,
            args=(profile, generation),
            name="vllm-worker-stop",
            daemon=True,
        ).start()
        return self.snapshot(profile)

    def _stop_worker(
        self,
        profile: ModelProfile,
        generation: int,
    ) -> None:
        try:
            with self._lock:
                process = self._process
            if process is not None and process.poll() is None:
                self._terminate_process(process)
            with self._lock:
                if self._process is process:
                    self._process = None
            self._set_state(
                "unloaded",
                "vLLM manager is ready; model worker is stopped",
                profile=profile,
                generation=generation,
            )
        except Exception as exc:
            self._set_state(
                "error",
                "vLLM worker could not be stopped cleanly",
                error=self._sanitize(str(exc)),
                profile=profile,
                generation=generation,
            )

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=self.settings.shutdown_timeout_sec)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        except ProcessLookupError:
            return

    def request_sleep(self, model_id: str = "") -> dict[str, Any]:
        profile = self._resolve_profile(model_id)
        if not self.settings.sleep_enabled:
            raise ValueError("vLLM sleep mode is disabled")
        with self._lock:
            if not self._profile_is_active(profile):
                raise ValueError(
                    f"{profile.display_name} is not the active vLLM model"
                )
            if self._state == "sleeping":
                return self.snapshot(profile)
            if self._state != "loaded":
                raise ValueError(f"Cannot sleep model while state is {self._state}")
            self._transition_generation += 1
            generation = self._transition_generation
            self._state = "suspending"
            self._detail = f"Offloading {profile.display_name} to host memory"
            self._last_error = ""
            self._updated_at = time.time()
        threading.Thread(
            target=self._sleep_worker,
            args=(profile, generation),
            name="vllm-worker-sleep",
            daemon=True,
        ).start()
        return self.snapshot(profile)

    def _sleep_worker(
        self,
        profile: ModelProfile,
        generation: int,
    ) -> None:
        try:
            response = requests.post(
                f"{self.worker_url}/sleep",
                params={"level": 1},
                headers=self._headers(),
                timeout=self.settings.request_timeout_sec,
            )
            response.raise_for_status()
            self._set_state(
                "sleeping",
                f"{profile.display_name} is sleeping in host memory",
                profile=profile,
                generation=generation,
            )
        except Exception as exc:
            self._set_state(
                "error",
                "vLLM worker failed to enter sleep mode",
                error=self._sanitize(str(exc)),
                profile=profile,
                generation=generation,
            )

    def request_wake(self, model_id: str = "") -> dict[str, Any]:
        profile = self._resolve_profile(model_id)
        with self._lock:
            if not self._profile_is_active(profile):
                raise ValueError(
                    f"{profile.display_name} is not the active vLLM model"
                )
            if self._state == "loaded":
                return self.snapshot(profile)
            if self._state == "waking":
                return self.snapshot(profile)
            if self._state != "sleeping":
                raise ValueError(f"Cannot wake model while state is {self._state}")
            self._transition_generation += 1
            generation = self._transition_generation
            self._state = "waking"
            self._detail = f"Restoring {profile.display_name} to GPU memory"
            self._last_error = ""
            self._updated_at = time.time()
        threading.Thread(
            target=self._wake_worker,
            args=(profile, generation),
            name="vllm-worker-wake",
            daemon=True,
        ).start()
        return self.snapshot(profile)

    def _wake_worker(
        self,
        profile: ModelProfile,
        generation: int,
    ) -> None:
        try:
            response = requests.post(
                f"{self.worker_url}/wake_up",
                headers=self._headers(),
                timeout=self.settings.request_timeout_sec,
            )
            response.raise_for_status()
            deadline = time.monotonic() + self.settings.startup_timeout_sec
            while time.monotonic() < deadline:
                response = requests.get(
                    f"{self.worker_url}/health",
                    headers=self._headers(),
                    timeout=2.0,
                )
                if response.ok:
                    self._set_state(
                        "loaded",
                        f"{profile.display_name} is ready for inference",
                        profile=profile,
                        generation=generation,
                    )
                    return
                time.sleep(0.5)
            raise TimeoutError("vLLM worker did not become ready after wake")
        except Exception as exc:
            self._set_state(
                "error",
                "vLLM worker failed to wake",
                error=self._sanitize(str(exc)),
                profile=profile,
                generation=generation,
            )

    def _tail_log(self, max_chars: int = 1200) -> str:
        try:
            data = self.settings.log_path.read_bytes()
        except OSError:
            return "worker log unavailable"
        return self._sanitize(data[-max_chars:].decode("utf-8", errors="replace"))

    def _sanitize(self, value: str) -> str:
        if self.settings.api_key:
            return value.replace(self.settings.api_key, "[redacted]")
        return value


settings = Settings.from_environment()
manager = RuntimeManager(settings)
app = FastAPI(title="Taskplanner vLLM Manager", version="0.2.0")


def require_auth(request: Request) -> None:
    if not settings.api_key:
        return
    expected = f"Bearer {settings.api_key}"
    if request.headers.get("authorization", "") != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.on_event("startup")
def on_startup() -> None:
    if settings.auto_start:
        manager.request_load()


@app.on_event("shutdown")
def on_shutdown() -> None:
    manager.request_unload(str(manager.snapshot()["model_id"]))
    deadline = time.monotonic() + settings.shutdown_timeout_sec + 2.0
    while (
        manager.snapshot()["state"] == "unloading"
        and time.monotonic() < deadline
    ):
        time.sleep(0.1)


@app.get("/health")
def health() -> dict[str, Any]:
    snapshot = manager.snapshot()
    return {
        "manager": "ready",
        "model_state": snapshot["state"],
        "model_id": snapshot["model_id"],
    }


@app.get("/manager/status", dependencies=[Depends(require_auth)])
def manager_status() -> dict[str, Any]:
    snapshot = manager.snapshot()
    snapshot["available_actions"] = manager.available_actions(
        state=str(snapshot["state"])
    )
    snapshot["catalog_size"] = len(settings.profiles)
    return snapshot


@app.post("/manager/load", status_code=202, dependencies=[Depends(require_auth)])
def load_model(body: ModelRequest) -> dict[str, Any]:
    try:
        return manager.request_load(body.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/manager/unload", status_code=202, dependencies=[Depends(require_auth)])
def unload_model(body: ModelRequest) -> dict[str, Any]:
    try:
        return manager.request_unload(body.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/manager/sleep", status_code=202, dependencies=[Depends(require_auth)])
def sleep_model(body: ModelRequest) -> dict[str, Any]:
    try:
        return manager.request_sleep(body.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/manager/wake", status_code=202, dependencies=[Depends(require_auth)])
def wake_model(body: ModelRequest) -> dict[str, Any]:
    try:
        return manager.request_wake(body.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/v1/models", dependencies=[Depends(require_auth)])
def list_models() -> dict[str, Any]:
    return {"object": "list", "data": manager.model_rows()}


@app.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    dependencies=[Depends(require_auth)],
)
async def proxy_openai(path: str, request: Request) -> Response:
    snapshot = manager.snapshot()
    if snapshot["state"] != "loaded":
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "type": "model_not_ready",
                    "code": str(snapshot["state"]),
                    "message": str(snapshot["detail"]),
                }
            },
        )
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection"}
    }
    body = await request.body()
    client = httpx.AsyncClient(timeout=settings.request_timeout_sec)
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                f"{manager.worker_url}/v1/{path}",
                params=request.query_params,
                content=body,
                headers=headers,
            ),
            stream=True,
        )
    except Exception:
        await client.aclose()
        raise
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower()
        not in {"content-length", "connection", "transfer-encoding"}
    }

    async def stream_upstream():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_upstream(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.manager_port,
        log_level=os.environ.get("VLLM_MANAGER_LOG_LEVEL", "info").lower(),
    )
