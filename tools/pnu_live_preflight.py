#!/usr/bin/env python3
"""Fail-closed preflight helpers for the PNU live perception pipeline.

This utility deliberately has no ROS or third-party Python dependency.  It is
intended to run in the Taskplanner environment and to validate model files,
worker discovery, and a versioned inference response.  A response with zero
detections is acceptable only when every requested algorithm proves that it
was ready *and* executed for the same source frame.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = REPOSITORY_ROOT / "reports/pnu_perception_preflight"
DEFAULT_ARTIFACT_MANIFEST = DEFAULT_REPORT_DIR / "artifact_manifest.json"
DEFAULT_ALGORITHMS = ("tool", "blood", "hand")
MODEL_ENVIRONMENT_KEYS = {
    "tool": "PNU_TOOL_CHECKPOINT",
    "blood": "PNU_BLOOD_CHECKPOINT",
    "hand": "PNU_HAND_MODEL",
}
MODEL_MOUNT_TARGET = "/models"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
INFERENCE_RESPONSE_SCHEMA = "taskplanner.pnu_perception.response.v1"
RESULT_COLLECTION_KEYS = {
    "tool": "detections",
    "blood": "detections",
    "hand": "hands",
}

EXIT_INVALID = 2
EXIT_MODEL_NOT_READY = 3
EXIT_MODEL_NOT_EXECUTED = 4
EXIT_ENDPOINT = 5


class PreflightError(RuntimeError):
    """A deterministic validation failure with an automation-safe code."""

    def __init__(self, error_code: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.exit_code = exit_code


@dataclass(frozen=True)
class AcceptanceOutcome:
    accepted: bool
    error_code: str
    exit_code: int
    summary: Mapping[str, Any]


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _load_json(path_text: str) -> Any:
    if path_text == "-":
        return json.load(sys.stdin)
    with Path(path_text).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_endpoint(
    endpoint: str,
    location: str,
    allow_insecure_remote_http: bool = False,
) -> str:
    parsed = urllib.parse.urlsplit(endpoint.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PreflightError(
            "INVALID_ENDPOINT",
            "endpoint must be an absolute HTTP(S) URL",
            EXIT_ENDPOINT,
        )
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PreflightError(
            "INVALID_ENDPOINT",
            "endpoint must not contain credentials, query, or fragment",
            EXIT_ENDPOINT,
        )
    if parsed.path not in {"", "/"}:
        raise PreflightError(
            "INVALID_ENDPOINT",
            "endpoint must be an origin without an API path",
            EXIT_ENDPOINT,
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise PreflightError(
            "INVALID_ENDPOINT",
            "endpoint has an invalid port",
            EXIT_ENDPOINT,
        ) from error

    host = parsed.hostname
    normalized_host = host.casefold().rstrip(".")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        address = None
        if normalized_host in {"localhost", "localhost.localdomain"}:
            addresses = (ipaddress.ip_address("127.0.0.1"),)
        else:
            try:
                resolved = socket.getaddrinfo(
                    normalized_host,
                    None,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                )
            except socket.gaierror as error:
                raise PreflightError(
                    "ENDPOINT_HOST_UNRESOLVED",
                    "endpoint hostname must resolve during preflight",
                    EXIT_ENDPOINT,
                ) from error
            addresses = tuple(
                {ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in resolved}
            )
            if not addresses:
                raise PreflightError(
                    "ENDPOINT_HOST_UNRESOLVED",
                    "endpoint hostname resolved to no IP addresses",
                    EXIT_ENDPOINT,
                )
    else:
        addresses = (address,)

    loopback = all(item.is_loopback for item in addresses)
    forbidden_remote = any(
        item.is_loopback or item.is_unspecified for item in addresses
    )

    if location == "local" and not loopback:
        raise PreflightError(
            "LOCAL_ENDPOINT_NOT_LOOPBACK",
            "local worker endpoint must resolve by an explicit loopback host",
            EXIT_ENDPOINT,
        )
    if location == "remote" and forbidden_remote:
        raise PreflightError(
            "REMOTE_ENDPOINT_NOT_LAN",
            "remote worker endpoint must not be loopback or unspecified",
            EXIT_ENDPOINT,
        )
    if (
        location == "remote"
        and parsed.scheme != "https"
        and not allow_insecure_remote_http
    ):
        raise PreflightError(
            "INSECURE_REMOTE_TRANSPORT",
            "remote worker endpoint must use HTTPS unless isolated trusted-LAN "
            "development explicitly allows HTTP",
            EXIT_ENDPOINT,
        )
    if location not in {"local", "remote"}:
        raise PreflightError(
            "INVALID_LOCATION", "location must be local or remote", EXIT_ENDPOINT
        )

    netloc = f"[{host}]" if address is not None and address.version == 6 else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, "", "", ""))


def _decode_api_token(payload: bytes) -> str:
    if not payload or len(payload) > 4096 or b"\x00" in payload:
        raise PreflightError(
            "INVALID_API_TOKEN_FILE",
            "API token file must contain 1..4096 UTF-8 bytes",
            EXIT_ENDPOINT,
        )
    try:
        token = payload.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise PreflightError(
            "INVALID_API_TOKEN_FILE",
            "API token file must contain UTF-8 text",
            EXIT_ENDPOINT,
        ) from error
    if not token or any(character.isspace() for character in token):
        raise PreflightError(
            "INVALID_API_TOKEN_FILE",
            "API token must be non-empty and contain no whitespace",
            EXIT_ENDPOINT,
        )
    return token


def _read_api_token(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise PreflightError(
            "INVALID_API_TOKEN_FILE",
            f"cannot read API token file: {path}",
            EXIT_ENDPOINT,
        ) from error
    return _decode_api_token(payload)


def _get_json(
    url: str, timeout_sec: float, api_token: str | None = None
) -> Mapping[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "taskplanner-pnu-preflight/1",
    }
    if api_token is not None:
        headers["Authorization"] = f"Bearer {api_token}"
    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise PreflightError(
                    "ENDPOINT_RESPONSE_TOO_LARGE",
                    f"{url} exceeded the 2 MiB preflight response limit",
                    EXIT_ENDPOINT,
                )
    except (urllib.error.URLError, TimeoutError) as error:
        raise PreflightError(
            "ENDPOINT_UNREACHABLE", f"GET {url} failed: {error}", EXIT_ENDPOINT
        ) from error
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightError(
            "ENDPOINT_INVALID_JSON", f"GET {url} did not return JSON", EXIT_ENDPOINT
        ) from error
    if not isinstance(payload, dict):
        raise PreflightError(
            "ENDPOINT_INVALID_JSON", f"GET {url} must return an object", EXIT_ENDPOINT
        )
    return payload


def _algorithm_names(capabilities: Mapping[str, Any]) -> set[str]:
    algorithms = capabilities.get("algorithms")
    if isinstance(algorithms, dict):
        return {str(name) for name in algorithms}
    if isinstance(algorithms, list):
        names: set[str] = set()
        for item in algorithms:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                names.add(item["name"])
        return names
    return set()


def _is_v1_schema(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value.endswith(".v1")


def check_worker(
    endpoint: str,
    location: str,
    timeout_sec: float,
    api_token_file: Path | None = None,
    allow_insecure_remote_http: bool = False,
    algorithms: Sequence[str] = DEFAULT_ALGORITHMS,
    expected_model_digests_json: str = "{}",
) -> Mapping[str, Any]:
    pins = validate_model_digest_pins(expected_model_digests_json, algorithms)
    origin = validate_endpoint(
        endpoint,
        location,
        allow_insecure_remote_http=allow_insecure_remote_http,
    )
    api_token = _read_api_token(api_token_file)
    health = _get_json(f"{origin}/v1/health", timeout_sec, api_token)
    capabilities = _get_json(f"{origin}/v1/capabilities", timeout_sec, api_token)
    requested = tuple(algorithms)
    missing = sorted(set(requested) - _algorithm_names(capabilities))
    if missing:
        raise PreflightError(
            "CAPABILITY_MISSING",
            f"worker does not advertise: {','.join(missing)}",
            EXIT_MODEL_NOT_READY,
        )
    if health.get("ready") is not True:
        raise PreflightError(
            "MODEL_NOT_READY",
            "worker health is reachable but ready is not true",
            EXIT_MODEL_NOT_READY,
        )
    observed_digests: dict[str, str] = {}
    for response_name, response_payload in (
        ("health", health),
        ("capabilities", capabilities),
    ):
        models = response_payload.get("models")
        if not isinstance(models, dict):
            raise PreflightError(
                "INVALID_WORKER_MODEL_IDENTITY",
                f"{response_name} response requires a models object",
                EXIT_MODEL_NOT_READY,
            )
        for algorithm in requested:
            record = models.get(algorithm)
            digest = record.get("digest_sha256") if isinstance(record, dict) else None
            if (
                not isinstance(record, dict)
                or record.get("ready") is not True
                or not isinstance(digest, str)
                or SHA256_PATTERN.fullmatch(digest) is None
            ):
                raise PreflightError(
                    "INVALID_WORKER_MODEL_IDENTITY",
                    f"{response_name} does not prove a ready {algorithm} model digest",
                    EXIT_MODEL_NOT_READY,
                )
            if digest != pins[algorithm]:
                raise PreflightError(
                    "WORKER_MODEL_DIGEST_MISMATCH",
                    f"{response_name} {algorithm} digest differs from the reviewed pin",
                    EXIT_MODEL_NOT_READY,
                )
            previous = observed_digests.setdefault(algorithm, digest)
            if previous != digest:
                raise PreflightError(
                    "WORKER_MODEL_DIGEST_MISMATCH",
                    f"worker responses disagree on the {algorithm} digest",
                    EXIT_MODEL_NOT_READY,
                )
    for name, payload in (("health", health), ("capabilities", capabilities)):
        if not _is_v1_schema(payload.get("schema")):
            raise PreflightError(
                "INVALID_WORKER_SCHEMA",
                f"{name} response requires a schema ending in .v1",
                EXIT_ENDPOINT,
            )
    auth = capabilities.get("auth")
    expected_auth_mode = "bearer" if api_token is not None else "none"
    if not isinstance(auth, dict) or auth.get("mode") != expected_auth_mode:
        raise PreflightError(
            "WORKER_AUTH_MODE_MISMATCH",
            "worker capabilities auth mode does not match the client token configuration",
            EXIT_ENDPOINT,
        )
    return {
        "accepted": True,
        "location": location,
        "origin": origin,
        "health_schema": health["schema"],
        "capabilities_schema": capabilities["schema"],
        "algorithms": sorted(_algorithm_names(capabilities)),
        "requested_algorithms": list(requested),
        "model_digests": observed_digests,
        "auth_mode": expected_auth_mode,
    }


def check_artifacts(manifest_path: Path) -> Mapping[str, Any]:
    manifest = _load_json(str(manifest_path))
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("artifacts"), list
    ):
        raise PreflightError(
            "INVALID_ARTIFACT_MANIFEST",
            "artifact manifest must contain an artifacts list",
            EXIT_INVALID,
        )

    rows: list[dict[str, Any]] = []
    blockers: list[str] = []
    for item in manifest["artifacts"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise PreflightError(
                "INVALID_ARTIFACT_MANIFEST",
                "each artifact requires a string id",
                EXIT_INVALID,
            )
        local = item.get("local") if isinstance(item.get("local"), dict) else {}
        path = (
            Path(local["path"]).expanduser()
            if isinstance(local.get("path"), str)
            else None
        )
        present = bool(path and path.is_file())
        row: dict[str, Any] = {"id": item["id"], "present": present}
        if present and path is not None:
            actual_size = path.stat().st_size
            actual_sha = _sha256(path)
            row.update(
                {"path": str(path), "size_bytes": actual_size, "sha256": actual_sha}
            )
            expected_size = item.get("expected_size_bytes")
            expected_sha = item.get("expected_sha256")
            row["size_matches"] = expected_size is None or actual_size == expected_size
            row["sha256_matches"] = (
                actual_sha == expected_sha if isinstance(expected_sha, str) else None
            )
            if not row["size_matches"] or not row["sha256_matches"]:
                reason = (
                    "authoritative SHA256 missing"
                    if expected_sha is None
                    else "integrity mismatch"
                )
                blockers.append(f"{item['id']}: {reason}")
        elif item.get("required_for_live") is True:
            blockers.append(f"{item['id']}: {item.get('download_status', 'missing')}")
        rows.append(row)

    return {
        "accepted": not blockers,
        "schema": "taskplanner.pnu_artifact_check.v1",
        "manifest": str(manifest_path),
        "artifacts": rows,
        "blockers": blockers,
    }


def validate_model_digest_pins(
    raw_json: str,
    algorithms: Sequence[str] = DEFAULT_ALGORITHMS,
) -> dict[str, str]:
    """Validate deployment-owned, non-TOFU model identities.

    Extra pins for another supported algorithm are allowed so a reviewed
    all-model map can safely be reused by a Debug subset. Every requested
    algorithm must still have an exact canonical lowercase SHA-256 value.
    """

    requested = tuple(algorithms)
    if not requested or len(set(requested)) != len(requested):
        raise PreflightError(
            "INVALID_ALGORITHM_SET",
            "algorithms must be a non-empty unique sequence",
            EXIT_INVALID,
        )
    unsupported = sorted(set(requested) - set(DEFAULT_ALGORITHMS))
    if unsupported:
        raise PreflightError(
            "INVALID_ALGORITHM_SET",
            f"unsupported algorithms: {','.join(unsupported)}",
            EXIT_INVALID,
        )
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate model digest key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(raw_json, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ValueError) as error:
        raise PreflightError(
            "INVALID_MODEL_DIGEST_PINS",
            "PNU_EXPECTED_MODEL_DIGESTS_JSON must be valid JSON",
            EXIT_INVALID,
        ) from error
    if not isinstance(payload, dict):
        raise PreflightError(
            "INVALID_MODEL_DIGEST_PINS",
            "PNU_EXPECTED_MODEL_DIGESTS_JSON must be an object",
            EXIT_INVALID,
        )
    unknown = sorted(set(payload) - set(DEFAULT_ALGORITHMS))
    if unknown:
        raise PreflightError(
            "INVALID_MODEL_DIGEST_PINS",
            f"model digest pins contain unsupported algorithms: {','.join(unknown)}",
            EXIT_INVALID,
        )
    missing = sorted(set(requested) - set(payload))
    if missing:
        raise PreflightError(
            "MODEL_DIGEST_PIN_MISSING",
            "full SHA-256 pins are required before startup for: "
            + ",".join(missing),
            EXIT_MODEL_NOT_READY,
        )
    invalid = sorted(
        name
        for name, digest in payload.items()
        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None
    )
    if invalid:
        raise PreflightError(
            "INVALID_MODEL_DIGEST_PINS",
            "model digest pins must be exact 64-character lowercase SHA-256 for: "
            + ",".join(invalid),
            EXIT_INVALID,
        )
    return {str(name): str(digest) for name, digest in payload.items()}


def _compose_service(
    config: Mapping[str, Any], service_name: str
) -> Mapping[str, Any]:
    services = config.get("services")
    service = services.get(service_name) if isinstance(services, dict) else None
    if not isinstance(service, dict):
        raise PreflightError(
            "INVALID_COMPOSE_CONFIG",
            f"Compose service is missing: {service_name}",
            EXIT_INVALID,
        )
    return service


def check_compose_model_pins(
    config: Mapping[str, Any],
    consumer_service: str,
    worker_service: str,
    algorithms: Sequence[str] = DEFAULT_ALGORITHMS,
    verify_local_files: bool = False,
) -> Mapping[str, Any]:
    """Validate pins from resolved Compose configuration and optional files."""

    consumer = _compose_service(config, consumer_service)
    consumer_environment = consumer.get("environment")
    if not isinstance(consumer_environment, dict):
        raise PreflightError(
            "INVALID_COMPOSE_CONFIG",
            f"Compose environment is missing: {consumer_service}",
            EXIT_INVALID,
        )
    raw_pins = consumer_environment.get("PNU_EXPECTED_MODEL_DIGESTS_JSON")
    if not isinstance(raw_pins, str):
        raise PreflightError(
            "INVALID_MODEL_DIGEST_PINS",
            "PNU_EXPECTED_MODEL_DIGESTS_JSON must resolve to a JSON string",
            EXIT_INVALID,
        )
    pins = validate_model_digest_pins(raw_pins, algorithms)
    requested = tuple(algorithms)

    verified_files: dict[str, dict[str, Any]] = {}
    if verify_local_files:
        worker = _compose_service(config, worker_service)
        worker_environment = worker.get("environment")
        if not isinstance(worker_environment, dict):
            raise PreflightError(
                "INVALID_COMPOSE_CONFIG",
                f"Compose environment is missing: {worker_service}",
                EXIT_INVALID,
            )
        volumes = worker.get("volumes")
        if not isinstance(volumes, list):
            volumes = []
        model_mounts = [
            volume
            for volume in volumes
            if isinstance(volume, dict)
            and volume.get("type") == "bind"
            and volume.get("target") == MODEL_MOUNT_TARGET
        ]
        if len(model_mounts) != 1 or model_mounts[0].get("read_only") is not True:
            raise PreflightError(
                "INVALID_MODEL_MOUNT",
                f"{worker_service} requires exactly one read-only bind at "
                f"{MODEL_MOUNT_TARGET}",
                EXIT_INVALID,
            )
        source_text = model_mounts[0].get("source")
        if not isinstance(source_text, str) or not source_text:
            raise PreflightError(
                "INVALID_MODEL_MOUNT",
                "model bind source must be a host path",
                EXIT_INVALID,
            )
        try:
            model_root = Path(source_text).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise PreflightError(
                "MODEL_ROOT_UNAVAILABLE",
                "configured local PNU model root is unavailable",
                EXIT_MODEL_NOT_READY,
            ) from error
        if not model_root.is_dir():
            raise PreflightError(
                "MODEL_ROOT_UNAVAILABLE",
                "configured local PNU model root is not a directory",
                EXIT_MODEL_NOT_READY,
            )

        container_root = Path(MODEL_MOUNT_TARGET)
        for algorithm in requested:
            environment_key = MODEL_ENVIRONMENT_KEYS[algorithm]
            configured_path = worker_environment.get(environment_key)
            if not isinstance(configured_path, str):
                raise PreflightError(
                    "INVALID_MODEL_PATH",
                    f"{environment_key} must resolve to an absolute path",
                    EXIT_INVALID,
                )
            container_path = Path(configured_path)
            try:
                relative = container_path.relative_to(container_root)
            except ValueError as error:
                raise PreflightError(
                    "INVALID_MODEL_PATH",
                    f"{environment_key} must stay beneath {MODEL_MOUNT_TARGET}",
                    EXIT_INVALID,
                ) from error
            if not relative.parts:
                raise PreflightError(
                    "INVALID_MODEL_PATH",
                    f"{environment_key} must identify a model file",
                    EXIT_INVALID,
                )
            candidate = model_root.joinpath(*relative.parts)
            try:
                resolved_candidate = candidate.resolve(strict=True)
                resolved_candidate.relative_to(model_root)
            except (OSError, RuntimeError, ValueError) as error:
                raise PreflightError(
                    "MODEL_FILE_UNAVAILABLE",
                    f"configured {algorithm} model is missing or escapes /models",
                    EXIT_MODEL_NOT_READY,
                ) from error
            if not resolved_candidate.is_file():
                raise PreflightError(
                    "MODEL_FILE_UNAVAILABLE",
                    f"configured {algorithm} model is not a regular file",
                    EXIT_MODEL_NOT_READY,
                )
            actual_digest = _sha256(resolved_candidate)
            if actual_digest != pins[algorithm]:
                raise PreflightError(
                    "MODEL_DIGEST_MISMATCH",
                    f"configured {algorithm} model does not match its reviewed SHA-256",
                    EXIT_MODEL_NOT_READY,
                )
            verified_files[algorithm] = {
                "path": str(resolved_candidate),
                "size_bytes": resolved_candidate.stat().st_size,
                "sha256": actual_digest,
            }

    return {
        "accepted": True,
        "schema": "taskplanner.pnu_model_pin_check.v1",
        "consumer_service": consumer_service,
        "worker_service": worker_service if verify_local_files else None,
        "requested_algorithms": list(requested),
        "expected_model_digests": {
            algorithm: pins[algorithm] for algorithm in requested
        },
        "local_files_verified": verify_local_files,
        "verified_files": verified_files,
    }


def _nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    )


def _requested_algorithms(value: str) -> tuple[str, ...]:
    names = tuple(part.strip() for part in value.split(",") if part.strip())
    if not names or len(set(names)) != len(names):
        raise PreflightError(
            "INVALID_ALGORITHM_SET",
            "algorithms must be a non-empty comma-separated unique list",
            EXIT_INVALID,
        )
    return names


def validate_inference_result(
    payload: Any,
    algorithms: Sequence[str] = DEFAULT_ALGORITHMS,
    expected_request_id: str | None = None,
    expected_source_stamp_ns: int | None = None,
    require_metric_3d: bool = False,
) -> AcceptanceOutcome:
    invalid: list[str] = []
    not_ready: list[str] = []
    not_executed: list[str] = []
    counts: dict[str, int] = {}

    if not isinstance(payload, dict):
        return AcceptanceOutcome(
            False,
            "INVALID_RESULT",
            EXIT_INVALID,
            {"issues": ["root must be an object"]},
        )

    schema = payload.get("schema")
    request_id = payload.get("request_id")
    if schema != INFERENCE_RESPONSE_SCHEMA:
        invalid.append(f"schema must be {INFERENCE_RESPONSE_SCHEMA}")
    if not isinstance(request_id, str) or not request_id:
        invalid.append("request_id is required")
    elif expected_request_id is not None and request_id != expected_request_id:
        invalid.append("request_id does not match the submitted request")

    generated_unix_ms = payload.get("generated_unix_ms")
    if (
        not isinstance(generated_unix_ms, int)
        or isinstance(generated_unix_ms, bool)
        or generated_unix_ms <= 0
    ):
        invalid.append("generated_unix_ms must be a positive integer")

    source = payload.get("source")
    rgb_source = source.get("rgb") if isinstance(source, dict) else None
    source_stamp_ns = (
        rgb_source.get("stamp_ns") if isinstance(rgb_source, dict) else None
    )
    if (
        not isinstance(source_stamp_ns, int)
        or isinstance(source_stamp_ns, bool)
        or source_stamp_ns <= 0
    ):
        invalid.append("source.rgb.stamp_ns must be a positive integer")
    elif (
        expected_source_stamp_ns is not None
        and source_stamp_ns != expected_source_stamp_ns
    ):
        invalid.append("input source stamp does not match the submitted frame")

    accepted_algorithms = payload.get("accepted_algorithms")
    if accepted_algorithms != list(algorithms):
        invalid.append("accepted_algorithms must exactly match the submitted order")

    models = payload.get("models")
    if not isinstance(models, dict):
        invalid.append("models must be an object")
        models = {}
    results = payload.get("results")
    if not isinstance(results, dict):
        invalid.append("results must be an object")
        results = {}
    elif set(results) != set(algorithms):
        invalid.append("results must contain exactly the accepted algorithms")
    latency = payload.get("latency_ms")
    if not isinstance(latency, dict):
        invalid.append("latency_ms must be an object")
        latency = {}
    for key in ("decode", "total"):
        if not _nonnegative_number(latency.get(key)):
            invalid.append(f"latency_ms.{key} must be non-negative")

    for name in algorithms:
        model = models.get(name)
        if not isinstance(model, dict):
            invalid.append(f"{name}: model record is missing")
            model = {}
        ready = model.get("ready")
        model_executed = model.get("executed")
        if ready is False:
            not_ready.append(name)
        elif ready is not True:
            invalid.append(f"{name}: model ready must be boolean")
        if ready is True:
            if model_executed is False:
                not_executed.append(name)
            elif model_executed is not True:
                invalid.append(f"{name}: model executed must be boolean")
            if model.get("status") != "executed" and name not in not_executed:
                not_executed.append(name)
            if model.get("error") not in (None, ""):
                invalid.append(f"{name}: model reported an error")

        result = results.get(name)
        if not isinstance(result, dict):
            invalid.append(f"{name}: result object is missing")
            continue
        result_executed = result.get("executed")
        if result_executed is False:
            if name not in not_executed:
                not_executed.append(name)
        elif result_executed is not True:
            invalid.append(f"{name}: result executed must be boolean")
        if not _nonnegative_number(latency.get(name)):
            invalid.append(f"latency_ms.{name} must be non-negative")
        result_schema = result.get("schema")
        if result_schema not in {f"pnu.{name}.2d.v1", f"pnu.{name}.rgbd.v1"}:
            invalid.append(f"{name}: result schema is invalid")
        collection_key = RESULT_COLLECTION_KEYS.get(name)
        collection = result.get(collection_key) if collection_key else None
        if not isinstance(collection, list):
            invalid.append(
                f"{name}: {collection_key or 'result collection'} must be a list, "
                "including for zero results"
            )
        else:
            counts[name] = len(collection)

    metric_3d = payload.get("metric_3d")
    metric_ready: bool | None = None
    metric_reasons: Any = None
    if not isinstance(metric_3d, dict):
        invalid.append("metric_3d must be an object")
    else:
        metric_ready = metric_3d.get("ready")
        metric_reasons = metric_3d.get("reasons")
        if not isinstance(metric_ready, bool):
            invalid.append("metric_3d.ready must be boolean")
        if not isinstance(metric_reasons, list) or any(
            not isinstance(reason, str) for reason in metric_reasons
        ):
            invalid.append("metric_3d.reasons must be a string list")

    depth_evidence = payload.get("depth_evidence")
    if not isinstance(depth_evidence, dict):
        invalid.append("depth_evidence must be an object")
        depth_evidence = {}
    if require_metric_3d and metric_ready is True:
        required_true = (
            "received",
            "decoded",
            "alignment_validated",
            "depth_scale_validated",
        )
        for key in required_true:
            if depth_evidence.get(key) is not True:
                invalid.append(f"depth_evidence.{key} must be true")
        if (
            not isinstance(depth_evidence.get("alignment_id"), str)
            or not str(depth_evidence.get("alignment_id", "")).strip()
        ):
            invalid.append("depth_evidence.alignment_id must be non-empty")
        if depth_evidence.get("rgb_frame_id") != depth_evidence.get("depth_frame_id"):
            invalid.append("depth_evidence RGB/depth frames must match")
        if depth_evidence.get("rgb_shape_hw") != depth_evidence.get("depth_shape_hw"):
            invalid.append("depth_evidence RGB/depth shapes must match")
        valid_pixels = depth_evidence.get("valid_pixels")
        if (
            not isinstance(valid_pixels, int)
            or isinstance(valid_pixels, bool)
            or valid_pixels <= 0
        ):
            invalid.append("depth_evidence.valid_pixels must be positive")

    summary: dict[str, Any] = {
        "schema": schema,
        "request_id": request_id,
        "input_source_stamp_ns": source_stamp_ns,
        "result_counts": counts,
        "total_results": sum(counts.values()),
        "metric_3d_ready": metric_ready,
        "metric_3d_reasons": metric_reasons,
        "issues": invalid,
        "not_ready": not_ready,
        "not_executed": not_executed,
    }
    if not_ready:
        return AcceptanceOutcome(
            False, "MODEL_NOT_READY", EXIT_MODEL_NOT_READY, summary
        )
    if not_executed:
        return AcceptanceOutcome(
            False, "MODEL_NOT_EXECUTED", EXIT_MODEL_NOT_EXECUTED, summary
        )
    if invalid:
        return AcceptanceOutcome(False, "INVALID_RESULT", EXIT_INVALID, summary)
    if require_metric_3d and metric_ready is not True:
        return AcceptanceOutcome(
            False,
            "METRIC_3D_NOT_READY",
            EXIT_MODEL_NOT_EXECUTED,
            summary,
        )
    summary["zero_results_accepted"] = summary["total_results"] == 0
    return AcceptanceOutcome(True, "", 0, summary)


def _self_test() -> Mapping[str, Any]:
    assert validate_endpoint("http://127.0.0.1:8020", "local") == (
        "http://127.0.0.1:8020"
    )
    assert validate_endpoint("https://192.168.1.20:8020", "remote") == (
        "https://192.168.1.20:8020"
    )
    try:
        validate_endpoint("http://192.168.1.20:8020", "remote")
    except PreflightError as error:
        assert error.error_code == "INSECURE_REMOTE_TRANSPORT"
    else:
        raise AssertionError("insecure remote HTTP endpoint was accepted")
    assert validate_endpoint(
        "http://192.168.1.20:8020",
        "remote",
        allow_insecure_remote_http=True,
    ) == "http://192.168.1.20:8020"

    assert _decode_api_token(b"self-test-token\n") == "self-test-token"
    for invalid_token in (b"", b"two tokens", b"\xff", b"x" * 4097):
        try:
            _decode_api_token(invalid_token)
        except PreflightError as error:
            assert error.error_code == "INVALID_API_TOKEN_FILE"
        else:
            raise AssertionError("invalid API token was accepted")

    base: dict[str, Any] = {
        "schema": INFERENCE_RESPONSE_SCHEMA,
        "request_id": "self-test",
        "generated_unix_ms": 1_787_242_400_000,
        "source": {
            "rgb": {
                "stamp_ns": 123,
                "frame_id": "cam_4_color_optical_frame",
                "format": "jpeg",
            }
        },
        "accepted_algorithms": list(DEFAULT_ALGORITHMS),
        "models": {
            name: {
                "ready": True,
                "executed": True,
                "status": "executed",
                "version": "self-test",
                "digest_sha256": "a" * 64,
                "backend": "self-test",
                "error": None,
            }
            for name in DEFAULT_ALGORITHMS
        },
        "latency_ms": {
            "decode": 1.0,
            "tool": 1.0,
            "blood": 1.0,
            "hand": 1.0,
            "total": 4.0,
        },
        "results": {
            "tool": {
                "schema": "pnu.tool.2d.v1",
                "detections": [],
                "executed": True,
            },
            "blood": {
                "schema": "pnu.blood.2d.v1",
                "detections": [],
                "executed": True,
            },
            "hand": {
                "schema": "pnu.hand.2d.v1",
                "hands": [],
                "executed": True,
            },
        },
        "metric_3d": {"ready": False, "reasons": ["depth_missing"]},
        "depth_evidence": {
            "received": False,
            "decoded": False,
            "alignment_validated": False,
            "alignment_id": "",
            "rgb_frame_id": "cam_4_color_optical_frame",
            "depth_frame_id": "",
            "rgb_shape_hw": [720, 1280],
            "depth_shape_hw": None,
            "depth_scale_m_per_unit": 0.0,
            "depth_scale_validated": False,
            "valid_pixels": 0,
            "valid_ratio": 0.0,
        },
        "upstream": {
            "repository": "hanwae-py/hand-blood-tools",
            "commit": "0f9e93115b8cc1d470398c92e010e3fc6ef1de5d",
        },
        "depth_received": False,
    }
    accepted = validate_inference_result(base)
    assert accepted.accepted and accepted.summary["zero_results_accepted"] is True

    not_executed = json.loads(json.dumps(base))
    not_executed["models"]["blood"]["executed"] = False
    not_executed["models"]["blood"]["status"] = "loaded"
    not_executed["results"]["blood"]["executed"] = False
    outcome = validate_inference_result(not_executed)
    assert outcome.error_code == "MODEL_NOT_EXECUTED" and outcome.exit_code == 4

    not_ready = json.loads(json.dumps(base))
    not_ready["models"]["tool"]["ready"] = False
    not_ready["models"]["tool"]["executed"] = False
    not_ready["models"]["tool"]["status"] = "unavailable"
    outcome = validate_inference_result(not_ready)
    assert outcome.error_code == "MODEL_NOT_READY" and outcome.exit_code == 3

    malformed = json.loads(json.dumps(base))
    del malformed["results"]["hand"]["hands"]
    outcome = validate_inference_result(malformed)
    assert outcome.error_code == "INVALID_RESULT" and outcome.exit_code == 2

    metric_ready = json.loads(json.dumps(base))
    metric_ready["metric_3d"] = {"ready": True, "reasons": []}
    metric_ready["source"]["depth"] = {
        "stamp_ns": 123,
        "frame_id": "cam_4_color_optical_frame",
        "format": "16UC1; compressedDepth png",
        "aligned": True,
    }
    metric_ready["depth_evidence"].update(
        {
            "received": True,
            "decoded": True,
            "alignment_validated": True,
            "alignment_id": "self-test-rgbd",
            "depth_frame_id": "cam_4_color_optical_frame",
            "depth_shape_hw": [720, 1280],
            "depth_scale_m_per_unit": 0.001,
            "depth_scale_validated": True,
            "valid_pixels": 700_000,
            "valid_ratio": 0.7595,
        }
    )
    for name in DEFAULT_ALGORITHMS:
        metric_ready["results"][name]["schema"] = f"pnu.{name}.rgbd.v1"
    outcome = validate_inference_result(metric_ready, require_metric_3d=True)
    assert outcome.accepted

    metric_not_ready = validate_inference_result(base, require_metric_3d=True)
    assert metric_not_ready.error_code == "METRIC_3D_NOT_READY"
    return {
        "accepted": True,
        "schema": "taskplanner.pnu_preflight_self_test.v1",
        "cases": [
            "remote_transport_policy",
            "api_token_file",
            "zero_detections",
            "model_not_executed",
            "model_not_ready",
            "invalid_result",
            "metric_3d_ready",
            "metric_3d_not_ready",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    artifacts = subparsers.add_parser("artifacts", help="verify local model artifacts")
    artifacts.add_argument("--manifest", type=Path, default=DEFAULT_ARTIFACT_MANIFEST)

    compose_pins = subparsers.add_parser(
        "compose-model-pins",
        help=(
            "validate required model SHA-256 pins from resolved Compose JSON; "
            "read the JSON from stdin by default"
        ),
    )
    compose_pins.add_argument("compose_config", nargs="?", default="-")
    compose_pins.add_argument("--consumer-service", required=True)
    compose_pins.add_argument("--worker-service", default="pnu-perception")
    compose_pins.add_argument("--algorithms", default=",".join(DEFAULT_ALGORITHMS))
    compose_pins.add_argument(
        "--verify-local-files",
        action="store_true",
        help="also hash the configured files through the read-only /models bind",
    )

    worker = subparsers.add_parser("worker", help="probe the versioned worker API")
    worker.add_argument("--endpoint", required=True)
    worker.add_argument("--location", choices=("local", "remote"), required=True)
    worker.add_argument("--timeout-sec", type=float, default=3.0)
    worker.add_argument("--algorithms", default=",".join(DEFAULT_ALGORITHMS))
    worker.add_argument(
        "--expected-model-digests-json",
        default=os.environ.get("PNU_EXPECTED_MODEL_DIGESTS_JSON", "{}"),
        help=(
            "reviewed full SHA-256 map; defaults to "
            "PNU_EXPECTED_MODEL_DIGESTS_JSON"
        ),
    )
    worker.add_argument(
        "--api-token-file",
        type=Path,
        help="read a bearer token from this file without placing it in argv",
    )
    worker.add_argument(
        "--allow-insecure-remote-http",
        action="store_true",
        help=(
            "development-only opt-in for plain HTTP to an isolated trusted-LAN "
            "remote worker"
        ),
    )

    result = subparsers.add_parser(
        "accept-result",
        help="accept a /v1/infer JSON response; use '-' to read stdin",
    )
    result.add_argument("result")
    result.add_argument("--algorithms", default=",".join(DEFAULT_ALGORITHMS))
    result.add_argument("--request-id")
    result.add_argument("--source-stamp-ns", type=int)
    result.add_argument(
        "--require-metric-3d",
        action="store_true",
        help="also require validated aligned depth with at least one metric sample",
    )

    subparsers.add_parser("self-test", help="run deterministic contract checks")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "artifacts":
            outcome = check_artifacts(args.manifest)
            _emit(outcome)
            return 0 if outcome["accepted"] else EXIT_MODEL_NOT_READY
        if args.command == "compose-model-pins":
            config = _load_json(args.compose_config)
            if not isinstance(config, dict):
                raise PreflightError(
                    "INVALID_COMPOSE_CONFIG",
                    "resolved Compose configuration must be an object",
                    EXIT_INVALID,
                )
            outcome = check_compose_model_pins(
                config,
                consumer_service=args.consumer_service,
                worker_service=args.worker_service,
                algorithms=_requested_algorithms(args.algorithms),
                verify_local_files=args.verify_local_files,
            )
            _emit(outcome)
            return 0
        if args.command == "worker":
            _emit(
                check_worker(
                    args.endpoint,
                    args.location,
                    args.timeout_sec,
                    args.api_token_file,
                    args.allow_insecure_remote_http,
                    _requested_algorithms(args.algorithms),
                    args.expected_model_digests_json,
                )
            )
            return 0
        if args.command == "accept-result":
            payload = _load_json(args.result)
            outcome = validate_inference_result(
                payload,
                algorithms=_requested_algorithms(args.algorithms),
                expected_request_id=args.request_id,
                expected_source_stamp_ns=args.source_stamp_ns,
                require_metric_3d=args.require_metric_3d,
            )
            _emit(
                {
                    "accepted": outcome.accepted,
                    "error_code": outcome.error_code,
                    **outcome.summary,
                }
            )
            return outcome.exit_code
        if args.command == "self-test":
            _emit(_self_test())
            return 0
        raise AssertionError(args.command)
    except (OSError, json.JSONDecodeError) as error:
        _emit({"accepted": False, "error_code": "INPUT_ERROR", "message": str(error)})
        return EXIT_INVALID
    except PreflightError as error:
        _emit(
            {
                "accepted": False,
                "error_code": error.error_code,
                "message": str(error),
            }
        )
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
