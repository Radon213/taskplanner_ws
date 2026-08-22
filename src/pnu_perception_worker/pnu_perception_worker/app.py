"""FastAPI boundary for binary PNU perception inference."""

from __future__ import annotations

import asyncio
import hmac
import json
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import (
    API_VERSION,
    CAPABILITIES_SCHEMA,
    HEALTH_SCHEMA,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
)
from .adapters import AdapterOutputError, AdapterRequestError, load_adapters
from .config import WorkerConfig
from .contract import ALGORITHMS, ContractError, parse_metadata
from .depth import InvalidDepthError
from .engine import (
    InferenceDeadlineError,
    InvalidImageError,
    ModelsUnavailableError,
    PerceptionEngine,
    WorkerBusyError,
)

ERROR_SCHEMA = "taskplanner.pnu_perception.error.v1"


def build_engine(config: WorkerConfig) -> PerceptionEngine:
    try:
        revision, adapters, errors = load_adapters(config)
    except Exception as exc:  # noqa: BLE001 - source verification must become degraded health
        revision = "unverified"
        adapters = {}
        message = f"{type(exc).__name__}: {exc}"
        errors = {name: message for name in ALGORITHMS}
    return PerceptionEngine(
        config,
        upstream_revision=revision,
        adapters=adapters,
        load_errors=errors,
    )


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    payload: dict[str, Any] = {
        "schema": ERROR_SCHEMA,
        "generated_unix_ms": int(time.time() * 1000),
        "error": {"code": code, "message": message},
    }
    payload["error"].update(extra)
    return JSONResponse(payload, status_code=status)


async def _read_bounded(upload: UploadFile, limit: int, *, label: str) -> bytes:
    payload = await upload.read(limit + 1)
    if len(payload) > limit:
        raise ContractError(f"{label} binary part exceeds {limit} bytes")
    if not payload:
        raise ContractError(f"{label} binary part is empty")
    if await upload.read(1):
        raise ContractError(f"{label} binary part exceeds {limit} bytes")
    return payload


def _authorize(request: Request, token: str | None) -> JSONResponse | None:
    if token is None:
        return None
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix) or not hmac.compare_digest(
        header[len(prefix) :], token
    ):
        response = _error(401, "unauthorized", "a valid bearer token is required")
        response.headers["WWW-Authenticate"] = "Bearer"
        return response
    return None


def create_app(
    engine: PerceptionEngine,
    *,
    api_token: str | None = None,
) -> FastAPI:
    config = engine.config
    app = FastAPI(
        title="Taskplanner PNU Perception Worker",
        version=API_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    ingress_lock = threading.Lock()

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        return {
            "schema": HEALTH_SCHEMA,
            "generated_unix_ms": int(time.time() * 1000),
            "status": "ready" if engine.ready else "degraded",
            "ready": engine.ready,
            "api_version": API_VERSION,
            "upstream": {
                "repository": "hanwae-py/hand-blood-tools",
                "expected_commit": config.expected_upstream_commit,
                "detected_commit": engine.upstream_revision,
            },
            "models": engine.model_records(),
        }

    @app.get("/v1/capabilities", response_model=None)
    def capabilities(request: Request) -> dict[str, Any] | JSONResponse:
        # Keep /v1/health usable by the local container healthcheck, but make
        # capabilities the side-effect-free bearer-token proof for a remote
        # client before any image bytes are accepted.
        auth_failure = _authorize(request, api_token)
        if auth_failure is not None:
            return auth_failure
        return {
            "schema": CAPABILITIES_SCHEMA,
            "generated_unix_ms": int(time.time() * 1000),
            "api_version": API_VERSION,
            "request_schema": REQUEST_SCHEMA,
            "response_schema": RESPONSE_SCHEMA,
            "transport": {
                "content_type": "multipart/form-data",
                "fields": {
                    "metadata": {
                        "required": True,
                        "content_type": "application/json",
                    },
                    "rgb": {
                        "required": True,
                        "content_types": ["image/jpeg", "image/png"],
                    },
                    "depth": {
                        "required": False,
                        "content_types": ["application/octet-stream", "image/png"],
                    },
                },
                "base64_allowed": False,
            },
            "execution": {
                "latest_frame_only": True,
                "max_in_flight": 1,
                "queue_depth": 0,
                "overload_status": 429,
            },
            "limits": {
                "request_bytes": config.max_request_bytes,
                "metadata_bytes": config.max_metadata_bytes,
                "rgb_bytes": config.max_rgb_bytes,
                "decoded_rgb_bytes": config.max_decoded_rgb_bytes,
                "depth_bytes": config.max_depth_bytes,
                "image_pixels": config.max_image_pixels,
                "detections_per_algorithm": config.max_detections_per_algorithm,
                "total_rle_counts_per_algorithm": config.max_total_rle_counts,
                "response_json_bytes": config.max_response_json_bytes,
                "deadline_ahead_ms": config.max_deadline_ahead_ms,
                "rgb_depth_skew_ns": config.max_rgb_depth_skew_ns,
            },
            "algorithms": list(ALGORITHMS),
            "models": engine.model_records(),
            "metric_3d": {
                "enabled": True,
                "reason": "enabled_for_validated_rgb_aligned_depth",
                "required_gates": [
                    "registered_or_alignment_validated_depth",
                    "alignment_validated_with_nonempty_id",
                    "matching_rgb_frame_and_dimensions",
                    "color_camera_info",
                    "matching_color_and_depth_camera_info",
                    "validated_depth_scale",
                ],
            },
            "auth": {"mode": "bearer" if api_token is not None else "none"},
        }

    @app.post("/v1/infer", response_model=None)
    async def infer(request: Request) -> Response:
        auth_failure = _authorize(request, api_token)
        if auth_failure is not None:
            return auth_failure

        raw_length = request.headers.get("content-length")
        if raw_length is None:
            return _error(411, "content_length_required", "Content-Length is required")
        try:
            content_length = int(raw_length)
        except ValueError:
            return _error(400, "invalid_content_length", "Content-Length is invalid")
        if content_length <= 0 or content_length > config.max_request_bytes:
            return _error(
                413,
                "request_too_large",
                f"request must be 1..{config.max_request_bytes} bytes",
            )
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            return _error(
                415,
                "unsupported_media_type",
                "Content-Type must be multipart/form-data with a boundary",
            )
        if not ingress_lock.acquire(blocking=False):
            response = _error(
                429,
                "worker_busy",
                "worker ingress is busy; retain and send only the latest frame",
            )
            response.headers["Retry-After"] = "0"
            return response

        try:
            try:
                form = await asyncio.wait_for(
                    request.form(
                        max_files=3,
                        max_fields=1,
                        max_part_size=max(
                            config.max_metadata_bytes,
                            config.max_depth_bytes,
                        ),
                    ),
                    timeout=config.max_ingress_read_sec,
                )
            except TimeoutError:
                return _error(
                    408,
                    "ingress_deadline_exceeded",
                    "multipart body did not arrive within the absolute ingress deadline",
                )
            entries = list(form.multi_items())
            names = [name for name, _value in entries]
            if len(names) != len(set(names)):
                raise ContractError("multipart field names must be unique")
            if set(names).difference({"metadata", "rgb", "depth"}):
                raise ContractError("multipart request contains unsupported fields")
            if "metadata" not in form or "rgb" not in form:
                raise ContractError("metadata and rgb parts are required")
            metadata_upload = form["metadata"]
            rgb_upload = form["rgb"]
            depth_upload = form.get("depth")
            if not isinstance(metadata_upload, UploadFile):
                raise ContractError("metadata must be a binary application/json part")
            if not isinstance(rgb_upload, UploadFile):
                raise ContractError("rgb must be a binary image part")
            if depth_upload is not None and not isinstance(depth_upload, UploadFile):
                raise ContractError("depth must be a binary part")
            if (metadata_upload.content_type or "").split(";", 1)[
                0
            ].lower() != "application/json":
                raise ContractError("metadata Content-Type must be application/json")
            rgb_content_type = (rgb_upload.content_type or "").split(";", 1)[0].lower()
            if rgb_content_type not in {"image/jpeg", "image/png"}:
                raise ContractError("rgb Content-Type must be image/jpeg or image/png")
            if depth_upload is not None:
                depth_content_type = (
                    (depth_upload.content_type or "").split(";", 1)[0].lower()
                )
                if depth_content_type not in {"application/octet-stream", "image/png"}:
                    raise ContractError(
                        "depth Content-Type must be application/octet-stream or image/png"
                    )
            metadata_bytes = await _read_bounded(
                metadata_upload, config.max_metadata_bytes, label="metadata"
            )
            rgb_bytes = await _read_bounded(
                rgb_upload, config.max_rgb_bytes, label="rgb"
            )
            depth_bytes = (
                await _read_bounded(depth_upload, config.max_depth_bytes, label="depth")
                if depth_upload is not None
                else None
            )
            parsed = parse_metadata(
                metadata_bytes,
                depth_present=depth_bytes is not None,
                max_deadline_ahead_ms=config.max_deadline_ahead_ms,
                max_rgb_depth_skew_ns=config.max_rgb_depth_skew_ns,
            )
            payload = await run_in_threadpool(
                engine.infer, parsed, rgb_bytes, depth_bytes
            )
            encoded_payload = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded_payload) > config.max_response_json_bytes:
                return _error(
                    507,
                    "response_too_large",
                    "inference result exceeds the advertised JSON response limit",
                )
            return Response(
                content=encoded_payload,
                status_code=200,
                media_type="application/json",
            )
        except ContractError as exc:
            return _error(422, "contract_error", str(exc))
        except StarletteHTTPException:
            return _error(
                422, "invalid_multipart", "multipart body is invalid or exceeds limits"
            )
        except ModelsUnavailableError as exc:
            return _error(
                503,
                "models_unavailable",
                str(exc),
                unavailable=exc.unavailable,
            )
        except WorkerBusyError as exc:
            response = _error(429, "worker_busy", str(exc))
            response.headers["Retry-After"] = "0"
            return response
        except InferenceDeadlineError as exc:
            return _error(504, "deadline_exceeded", str(exc))
        except InvalidImageError as exc:
            return _error(422, "invalid_rgb", str(exc))
        except InvalidDepthError as exc:
            return _error(422, "invalid_depth", str(exc))
        except AdapterRequestError:
            return _error(
                422,
                "invalid_perception_geometry",
                "request calibration or per-frame geometry is invalid",
            )
        except AdapterOutputError:
            return _error(
                422,
                "invalid_perception_output",
                "per-frame perception output exceeds reviewed limits",
            )
        except Exception as exc:  # noqa: BLE001 - sanitize the public framework boundary
            # The public response never contains a traceback, path, or request body.
            return _error(
                500,
                "inference_failed",
                f"{type(exc).__name__}: inference did not complete",
            )
        finally:
            ingress_lock.release()

    return app


def create_app_from_env() -> FastAPI:
    config = WorkerConfig.from_env()
    return create_app(build_engine(config), api_token=config.read_api_token())
