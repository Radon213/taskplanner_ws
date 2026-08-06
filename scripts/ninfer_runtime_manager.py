#!/usr/bin/env python3
"""Small on-demand control plane for a host-installed NInfer runtime.

The manager is intentionally lightweight: it starts with every model unloaded,
advertises installed artifacts through an OpenAI-compatible model catalog, and
launches one configured NInfer worker only after an explicit load request.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    display_name: str
    capability: str
    artifact_path: str
    start_command: tuple[str, ...]
    stop_command: tuple[str, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)

    @property
    def installed(self) -> bool:
        return bool(
            self.artifact_path
            and Path(self.artifact_path).expanduser().is_file()
        )


def _command_parts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(shlex.split(value))
    if isinstance(value, list):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _load_catalog(path: str) -> tuple[ModelSpec, ...]:
    if not path:
        return ()
    source = Path(path).expanduser()
    payload = json.loads(source.read_text(encoding="utf-8"))
    rows = payload.get("models", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("NInfer catalog must be a list or {models: [...]}")
    models: list[ModelSpec] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(
            row.get("id", row.get("model_id", ""))
        ).strip()
        if not model_id:
            raise ValueError("NInfer catalog model is missing id")
        environment = row.get("environment", {})
        models.append(
            ModelSpec(
                model_id=model_id,
                display_name=str(
                    row.get("display_name", model_id)
                ).strip(),
                capability=str(
                    row.get("capability", "vision")
                ).strip(),
                artifact_path=os.path.expandvars(
                    os.path.expanduser(
                        str(
                            row.get(
                                "artifact_path",
                                row.get("artifact", ""),
                            )
                        ).strip()
                    )
                ),
                start_command=_command_parts(row.get("start_command")),
                stop_command=_command_parts(row.get("stop_command")),
                environment={
                    str(key): os.path.expandvars(
                        os.path.expanduser(str(value))
                    )
                    for key, value in (
                        environment.items()
                        if isinstance(environment, dict)
                        else ()
                    )
                },
            )
        )
    return tuple(models)


def _load_env_files(paths: list[str]) -> None:
    """Load Compose-style env files while preserving exported shell values."""

    original_keys = set(os.environ)
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in original_keys:
                continue
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]
            os.environ[key] = os.path.expandvars(value)


class NInferRuntimeManager:
    def __init__(
        self,
        *,
        models: tuple[ModelSpec, ...],
        worker_host: str,
        worker_port: int,
        startup_timeout_sec: float,
        shutdown_timeout_sec: float,
        api_key: str,
    ) -> None:
        self.models = {model.model_id: model for model in models}
        self.worker_host = worker_host
        self.worker_port = int(worker_port)
        self.startup_timeout_sec = max(1.0, float(startup_timeout_sec))
        self.shutdown_timeout_sec = max(1.0, float(shutdown_timeout_sec))
        self.api_key = api_key.strip()
        self._lock = threading.RLock()
        self._states = {
            model_id: "unloaded" for model_id in self.models
        }
        self._details = {
            model_id: (
                "Installed and unloaded"
                if model.installed
                else f"Configured artifact is missing: {model.artifact_path}"
            )
            for model_id, model in self.models.items()
        }
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._threads: dict[str, threading.Thread] = {}

    @property
    def worker_base_url(self) -> str:
        return f"http://{self.worker_host}:{self.worker_port}"

    def authorized(self, authorization: str) -> bool:
        if not self.api_key:
            return True
        return authorization.strip() == f"Bearer {self.api_key}"

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            states = dict(self._states)
            details = dict(self._details)
        return {
            "service": "taskplanner-ninfer-manager",
            "ready": True,
            "worker_base_url": self.worker_base_url,
            "models": [
                {
                    "id": model.model_id,
                    "state": states[model.model_id],
                    "detail": details[model.model_id],
                    "installed": model.installed,
                }
                for model in self.models.values()
            ],
        }

    def model_catalog_payload(self) -> dict[str, Any]:
        with self._lock:
            states = dict(self._states)
            details = dict(self._details)
        return {
            "object": "list",
            "data": [
                {
                    "id": model.model_id,
                    "object": "model",
                    "owned_by": "ninfer",
                    "display_name": model.display_name,
                    "capability": model.capability,
                    "capabilities": {
                        "vision": model.capability == "vision",
                        "text": model.capability in {"vision", "text"},
                    },
                    "load_state": (
                        states[model.model_id]
                        if model.installed
                        else "error"
                    ),
                    "loaded": states[model.model_id] == "loaded",
                    "installed": model.installed,
                    "available": model.installed
                    and bool(model.start_command),
                    "runtime_managed": True,
                    "detail": details[model.model_id],
                }
                for model in self.models.values()
            ],
        }

    def active_model_id(self) -> str:
        with self._lock:
            for model_id, state in self._states.items():
                if state == "loaded":
                    return model_id
        return ""

    def request_load(self, model_id: str) -> tuple[int, dict[str, Any]]:
        model = self.models.get(model_id)
        if model is None:
            return 404, {
                "state": "error",
                "model_id": model_id,
                "detail": f"Unknown NInfer model: {model_id}",
            }
        if not model.installed:
            return 409, {
                "state": "error",
                "model_id": model_id,
                "detail": f"Configured artifact is missing: {model.artifact_path}",
            }
        if not model.start_command:
            return 409, {
                "state": "error",
                "model_id": model_id,
                "detail": "No NInfer start command is configured",
            }
        with self._lock:
            current = self._states[model_id]
            if current in {"loaded", "loading"}:
                return 202, {
                    "state": current,
                    "model_id": model_id,
                    "detail": self._details[model_id],
                }
            busy = [
                other_id
                for other_id, state in self._states.items()
                if other_id != model_id
                and state in {"loading", "loaded", "unloading"}
            ]
            if busy:
                return 409, {
                    "state": "busy",
                    "model_id": model_id,
                    "detail": (
                        "Unload the active NInfer model first: "
                        + ", ".join(busy)
                    ),
                }
            self._states[model_id] = "loading"
            self._details[model_id] = f"Loading {model.display_name}"
            thread = threading.Thread(
                target=self._load_worker,
                args=(model,),
                name=f"ninfer-load-{model_id}",
                daemon=True,
            )
            self._threads[model_id] = thread
            thread.start()
        return 202, {
            "state": "loading",
            "model_id": model_id,
            "detail": f"Loading {model.display_name}",
        }

    def request_unload(self, model_id: str) -> tuple[int, dict[str, Any]]:
        model = self.models.get(model_id)
        if model is None:
            return 404, {
                "state": "error",
                "model_id": model_id,
                "detail": f"Unknown NInfer model: {model_id}",
            }
        with self._lock:
            current = self._states[model_id]
            if current == "unloaded":
                return 200, {
                    "state": "unloaded",
                    "model_id": model_id,
                    "detail": "Model is already unloaded",
                }
            if current == "unloading":
                return 202, {
                    "state": current,
                    "model_id": model_id,
                    "detail": self._details[model_id],
                }
            self._states[model_id] = "unloading"
            self._details[model_id] = f"Unloading {model.display_name}"
            thread = threading.Thread(
                target=self._unload_worker,
                args=(model,),
                name=f"ninfer-unload-{model_id}",
                daemon=True,
            )
            self._threads[model_id] = thread
            thread.start()
        return 202, {
            "state": "unloading",
            "model_id": model_id,
            "detail": f"Unloading {model.display_name}",
        }

    def _expanded_command(
        self,
        command: tuple[str, ...],
        model: ModelSpec,
    ) -> list[str]:
        values = {
            "artifact": model.artifact_path,
            "model_id": model.model_id,
            "worker_host": self.worker_host,
            "worker_port": str(self.worker_port),
        }
        return [
            os.path.expandvars(part).format(**values)
            for part in command
        ]

    def _worker_is_ready(self, model_id: str) -> bool:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.worker_base_url}/v1/models",
            headers=headers,
        )
        try:
            with urlopen(request, timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError):
            return False
        rows = (
            payload.get("data", [])
            if isinstance(payload, dict)
            else []
        )
        return any(
            isinstance(row, dict)
            and str(row.get("id", "")).strip() == model_id
            for row in rows
        )

    def _load_worker(self, model: ModelSpec) -> None:
        environment = os.environ.copy()
        environment.update(model.environment)
        environment["NINFER_HOST"] = self.worker_host
        environment["NINFER_PORT"] = str(self.worker_port)
        environment["NINFER_MODEL_ID"] = model.model_id
        environment["NINFER_MODEL_ARTIFACT"] = model.artifact_path
        if self.api_key:
            environment["NINFER_API_KEY"] = self.api_key
        try:
            process = subprocess.Popen(
                self._expanded_command(model.start_command, model),
                env=environment,
                stdout=None,
                stderr=None,
                start_new_session=True,
            )
            with self._lock:
                self._processes[model.model_id] = process
            deadline = time.monotonic() + self.startup_timeout_sec
            while time.monotonic() < deadline:
                return_code = process.poll()
                if return_code is not None:
                    raise RuntimeError(
                        f"NInfer worker exited with code {return_code}"
                    )
                if self._worker_is_ready(model.model_id):
                    with self._lock:
                        self._states[model.model_id] = "loaded"
                        self._details[model.model_id] = (
                            f"{model.display_name} is loaded"
                        )
                        self._threads.pop(model.model_id, None)
                    return
                time.sleep(0.5)
            raise TimeoutError(
                f"NInfer worker did not become ready within "
                f"{self.startup_timeout_sec:.0f}s"
            )
        except Exception as exc:
            with self._lock:
                process = self._processes.pop(model.model_id, None)
            if process is not None and process.poll() is None:
                process.terminate()
            with self._lock:
                self._states[model.model_id] = "error"
                self._details[model.model_id] = str(exc)
                self._threads.pop(model.model_id, None)

    def _unload_worker(self, model: ModelSpec) -> None:
        error = ""
        try:
            if model.stop_command:
                completed = subprocess.run(
                    self._expanded_command(model.stop_command, model),
                    env={**os.environ, **model.environment},
                    capture_output=True,
                    text=True,
                    timeout=self.shutdown_timeout_sec,
                    check=False,
                )
                if completed.returncode != 0:
                    error = (
                        completed.stderr.strip()
                        or f"Stop command exited with {completed.returncode}"
                    )
            with self._lock:
                process = self._processes.pop(model.model_id, None)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=self.shutdown_timeout_sec)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
        except Exception as exc:
            error = str(exc)
        with self._lock:
            self._states[model.model_id] = (
                "error" if error else "unloaded"
            )
            self._details[model.model_id] = (
                error or "Installed and unloaded"
            )
            self._threads.pop(model.model_id, None)

    def shutdown(self) -> None:
        for model in self.models.values():
            with self._lock:
                state = self._states[model.model_id]
            if state in {"loaded", "loading", "unloading"}:
                self._unload_worker(model)


class ManagerRequestHandler(BaseHTTPRequestHandler):
    server_version = "TaskplannerNInferManager/1.0"

    @property
    def manager(self) -> NInferRuntimeManager:
        return self.server.manager  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            f"[ninfer-manager] {self.address_string()} "
            + fmt % args,
            flush=True,
        )

    def _json_response(
        self,
        status_code: int,
        payload: dict[str, Any],
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.manager.authorized(
            self.headers.get("Authorization", "")
        ):
            return True
        self._json_response(
            401,
            {"detail": "Authentication required"},
        )
        return False

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        payload = json.loads(
            self.rfile.read(content_length).decode("utf-8")
        )
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json_response(200, self.manager.status_payload())
            return
        if not self._authorized():
            return
        if self.path == "/manager/status":
            self._json_response(200, self.manager.status_payload())
            return
        if self.path == "/v1/models":
            self._json_response(
                200,
                self.manager.model_catalog_payload(),
            )
            return
        self._proxy()

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if self.path in {"/manager/load", "/manager/unload"}:
            try:
                payload = self._read_json()
                model_id = str(payload.get("model_id", "")).strip()
                if not model_id:
                    raise ValueError("model_id is required")
                if self.path.endswith("/load"):
                    status, response = self.manager.request_load(
                        model_id
                    )
                else:
                    status, response = self.manager.request_unload(
                        model_id
                    )
                self._json_response(status, response)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json_response(400, {"detail": str(exc)})
            return
        self._proxy()

    def _proxy(self) -> None:
        if not self.manager.active_model_id():
            self._json_response(
                503,
                {"detail": "No NInfer model is loaded"},
            )
            return
        body_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(body_length) if body_length else None
        connection = http.client.HTTPConnection(
            self.manager.worker_host,
            self.manager.worker_port,
            timeout=900.0,
        )
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
            and key.lower() not in {"host", "content-length"}
        }
        if self.manager.api_key:
            headers["Authorization"] = (
                f"Bearer {self.manager.api_key}"
            )
        try:
            connection.request(
                self.command,
                self.path,
                body=body,
                headers=headers,
            )
            response = connection.getresponse()
            response_body = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if (
                    key.lower() not in HOP_BY_HOP_HEADERS
                    and key.lower() != "content-length"
                ):
                    self.send_header(key, value)
            self.send_header(
                "Content-Length",
                str(len(response_body)),
            )
            self.end_headers()
            self.wfile.write(response_body)
        except OSError as exc:
            self._json_response(
                502,
                {"detail": f"NInfer worker proxy failed: {exc}"},
            )
        finally:
            connection.close()


def parse_args() -> argparse.Namespace:
    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument(
        "--env-file",
        action="append",
        default=[],
    )
    env_args, _ = env_parser.parse_known_args()
    _load_env_files(env_args.env_file)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--catalog",
        default=os.environ.get("NINFER_MODEL_CATALOG_PATH", ""),
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("NINFER_MANAGER_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("NINFER_MANAGER_PORT", "8080")),
    )
    parser.add_argument(
        "--worker-host",
        default=os.environ.get(
            "NINFER_MANAGER_WORKER_HOST",
            "127.0.0.1",
        ),
    )
    parser.add_argument(
        "--worker-port",
        type=int,
        default=int(
            os.environ.get("NINFER_MANAGER_WORKER_PORT", "8082")
        ),
    )
    parser.add_argument(
        "--startup-timeout-sec",
        type=float,
        default=float(
            os.environ.get(
                "NINFER_MANAGER_STARTUP_TIMEOUT_SEC",
                "600",
            )
        ),
    )
    parser.add_argument(
        "--shutdown-timeout-sec",
        type=float,
        default=float(
            os.environ.get(
                "NINFER_MANAGER_SHUTDOWN_TIMEOUT_SEC",
                "45",
            )
        ),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("NINFER_API_KEY", ""),
    )
    parser.add_argument("--print-base-url", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.print_base_url:
        print(f"http://{args.host}:{args.port}")
        return
    models = _load_catalog(args.catalog)
    manager = NInferRuntimeManager(
        models=models,
        worker_host=args.worker_host,
        worker_port=args.worker_port,
        startup_timeout_sec=args.startup_timeout_sec,
        shutdown_timeout_sec=args.shutdown_timeout_sec,
        api_key=args.api_key,
    )
    server = ThreadingHTTPServer(
        (args.host, args.port),
        ManagerRequestHandler,
    )
    server.manager = manager  # type: ignore[attr-defined]

    def request_shutdown(_signum: int, _frame: Any) -> None:
        threading.Thread(
            target=server.shutdown,
            name="ninfer-manager-shutdown",
            daemon=True,
        ).start()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    print(
        "Taskplanner NInfer manager listening at "
        f"http://{args.host}:{args.port}; "
        f"{len(models)} configured model(s), all unloaded",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        manager.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
