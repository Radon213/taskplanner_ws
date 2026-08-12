"""Post-operative surgery-record API test support for Debug Mode."""

from __future__ import annotations

from collections import deque
from datetime import date as date_type
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


MAX_TEXT_CHARS = 65_535
MAX_BODY_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_048_576
MAX_RESPONSE_TEXT_BYTES = 16_384
MAX_API_KEY_FILE_BYTES = 4_096
DEFAULT_TIMEOUT_SEC = 35.0
CASE_FILE_PATTERN = re.compile(r"^(0704_(?:[6-9]|1[0-7]))_surgery_record\.txt$")
SURGERY_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REDACTED = "[REDACTED]"


class ResponseTooLargeError(ValueError):
    """Raised when an API response exceeds the local inspection boundary."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _open_without_redirects(request: Request, *, timeout: float) -> Any:
    return build_opener(_NoRedirectHandler).open(request, timeout=timeout)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_endpoint(value: Any) -> str:
    endpoint = str(value or "").strip()
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("surgery-record endpoint must be an https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("credentials must not be embedded in the endpoint URL")
    if not parsed.path or parsed.path == "/":
        raise ValueError("surgery-record endpoint must include an API path")
    if parsed.query or parsed.fragment:
        raise ValueError("surgery-record endpoint must not include a query or fragment")
    return endpoint


def validate_request_fields(
    *, room_name: Any, surgery_code: Any, surgery_date: Any, text: Any
) -> dict[str, str]:
    if not isinstance(room_name, str):
        raise ValueError("roomName must be a string")
    if not isinstance(surgery_code, str):
        raise ValueError("surgeryCode must be a string")
    if not isinstance(surgery_date, str):
        raise ValueError("date must be a string")
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    room = room_name.strip()
    code = surgery_code.strip()
    date_value = surgery_date.strip()
    content = text
    if not room:
        raise ValueError("roomName is required")
    if len(room) > 100:
        raise ValueError("roomName must be at most 100 characters")
    if not SURGERY_CODE_PATTERN.fullmatch(code):
        raise ValueError("surgeryCode must contain only letters, numbers, _, or -")
    if not DATE_PATTERN.fullmatch(date_value):
        raise ValueError("date must use YYYY-MM-DD")
    try:
        date_type.fromisoformat(date_value)
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD") from exc
    if not content.strip():
        raise ValueError("text is required")
    if len(content) > MAX_TEXT_CHARS:
        raise ValueError(f"text must be at most {MAX_TEXT_CHARS:,} characters")
    return {
        "roomName": room,
        "surgeryCode": code,
        "date": date_value,
        "text": content,
    }


def _is_sensitive_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).lower())
    return (
        normalized == "auth"
        or normalized.startswith("auth")
        or any(
            marker in normalized
            for marker in ("apikey", "token", "secret", "password", "credential")
        )
    )


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTED)
    return redacted


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _redact_response(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): REDACTED
            if _is_sensitive_key(key)
            else _redact_response(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_response(item, secrets) for item in value]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    return value


def _decode_response(
    raw: bytes, *, secrets: tuple[str, ...] = ()
) -> tuple[dict[str, Any] | None, str]:
    text = raw.decode("utf-8", errors="replace")
    # Redact before bounding the browser-visible text. Truncating first could
    # leave a credential prefix visible when a reflected secret crosses the
    # truncation boundary.
    safe_text = _truncate_utf8(
        _redact_text(text, secrets), MAX_RESPONSE_TEXT_BYTES
    )
    try:
        parsed = json.loads(text)
    except ValueError:
        return None, safe_text
    normalized = parsed if isinstance(parsed, dict) else {"value": parsed}
    return _redact_response(normalized, secrets), safe_text


def _read_response(response: Any) -> bytes:
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ResponseTooLargeError(
            f"response body exceeds the {MAX_RESPONSE_BYTES:,}-byte inspection limit"
        )
    return raw


def _response_headers(response: Any, *, secrets: tuple[str, ...]) -> dict[str, str]:
    headers = getattr(response, "headers", None)
    if headers is None:
        return {}
    return {
        str(key).lower(): _redact_text(str(value), secrets)
        for key, value in headers.items()
        if str(key).lower() in {"content-type", "retry-after", "x-request-id"}
    }


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, OSError) and exc.errno == errno.ETIMEDOUT:
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException) and reason is not exc:
        return _is_timeout_error(reason)
    if isinstance(reason, str):
        return "timed out" in reason.lower() or "timeout" in reason.lower()
    return False


class SurgeryRecordRuntime:
    """Discover local examples and run one non-overlapping API test at a time."""

    def __init__(
        self,
        *,
        input_dir: str | Path,
        default_endpoint: str,
        api_key_file: str | Path | None = None,
        allowed_endpoints: tuple[str, ...] | list[str] | None = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        opener: Callable[..., Any] = _open_without_redirects,
    ) -> None:
        self._input_dir = Path(input_dir)
        self._api_key_file = Path(api_key_file) if api_key_file else None
        self._default_endpoint = validate_endpoint(default_endpoint)
        configured_endpoints = allowed_endpoints or (self._default_endpoint,)
        self._allowed_endpoints = tuple(
            dict.fromkeys(
                validate_endpoint(endpoint) for endpoint in configured_endpoints
            )
        )
        if self._default_endpoint not in self._allowed_endpoints:
            raise ValueError("default surgery-record endpoint must be allowlisted")
        self._timeout_sec = max(1.0, float(timeout_sec))
        self._opener = opener
        self._lock = threading.RLock()
        self._cases: list[dict[str, Any]] = []
        self._state = "IDLE"
        self._active_request_id = ""
        self._last_error = ""
        self._last_result: dict[str, Any] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=20)
        self._events: deque[dict[str, Any]] = deque(maxlen=80)
        self.refresh_cases()

    def _read_api_key(self) -> str:
        path = self._api_key_file
        if path is None:
            raise ValueError("server X-API-Key is not configured")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                file_stat = os.fstat(descriptor)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValueError("server X-API-Key is not configured securely")
                if stat.S_IMODE(file_stat.st_mode) != 0o600:
                    raise ValueError("server X-API-Key is not configured securely")
                if file_stat.st_uid != os.geteuid():
                    raise ValueError("server X-API-Key is not configured securely")
                raw = os.read(descriptor, MAX_API_KEY_FILE_BYTES + 1)
            finally:
                os.close(descriptor)
        except ValueError:
            raise
        except (OSError, UnicodeError) as exc:
            raise ValueError("server X-API-Key is not configured") from exc
        if len(raw) > MAX_API_KEY_FILE_BYTES:
            raise ValueError("server X-API-Key is not configured securely")
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError("server X-API-Key is not configured securely") from exc
        key = text.rstrip("\r\n")
        if not key or key != key.strip():
            raise ValueError("server X-API-Key is not configured")
        if "\r" in key or "\n" in key:
            raise ValueError("server X-API-Key file must contain exactly one line")
        return key

    def _api_key_configured(self) -> bool:
        try:
            self._read_api_key()
        except ValueError:
            return False
        return True

    def refresh_cases(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            candidates = sorted(
                self._input_dir.glob("*_surgery_record.txt"),
                key=lambda path: self._case_sort_key(path.name),
            )
            for path in candidates:
                match = CASE_FILE_PATTERN.fullmatch(path.name)
                if not match or not path.is_file():
                    continue
                raw = path.read_bytes()
                text = raw.decode("utf-8")
                rows.append(
                    {
                        "case_id": match.group(1),
                        "filename": path.name,
                        "characters": len(text),
                        "bytes": len(raw),
                        "lines": len(text.splitlines()),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "valid_for_api": (
                            len(text) <= MAX_TEXT_CHARS
                            and len(raw) < MAX_BODY_BYTES
                        ),
                    }
                )
            error = (
                ""
                if rows
                else f"No 0704_6-0704_17 TXT examples found in {self._input_dir}"
            )
        except Exception as exc:
            rows = []
            error = f"Failed to scan surgery-record examples: {exc}"
        with self._lock:
            self._cases = rows
            self._last_error = error
        return rows

    def submit_async(self, payload: dict[str, Any]) -> str:
        if "api_key" in payload:
            raise ValueError("X-API-Key must not be supplied by the browser")
        api_key = self._read_api_key()
        endpoint = validate_endpoint(payload.get("endpoint") or self._default_endpoint)
        if endpoint not in self._allowed_endpoints:
            raise ValueError("surgery-record endpoint is not in the configured allowlist")
        case_id = str(payload.get("case_id") or "").strip()
        content = payload.get("text", "")
        filename = "browser_input.txt"
        if case_id:
            content, filename = self._read_case(case_id)
        fields = validate_request_fields(
            room_name=payload.get("room_name"),
            surgery_code=payload.get("surgery_code") or case_id,
            surgery_date=payload.get("date"),
            text=content,
        )
        encoded = json.dumps(
            fields, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_BODY_BYTES:
            raise ValueError("encoded JSON body exceeds the documented 1 MB limit")
        request_id = f"record-{uuid4()}"
        safe_request = {
            "request_id": request_id,
            "case_id": case_id or "custom",
            "filename": filename,
            "endpoint": endpoint,
            "room_name": fields["roomName"],
            "surgery_code": fields["surgeryCode"],
            "date": fields["date"],
            "text_characters": len(fields["text"]),
            "body_bytes": len(encoded),
            "text_sha256": hashlib.sha256(fields["text"].encode("utf-8")).hexdigest(),
            "submitted_at": _utc_now(),
        }
        with self._lock:
            if self._state == "SUBMITTING":
                raise ValueError("another surgery-record request is still in progress")
            self._state = "SUBMITTING"
            self._active_request_id = request_id
            self._last_error = ""
            self._last_result = dict(safe_request)
            self._events.append({"type": "record_submit_started", **safe_request})
        thread = threading.Thread(
            target=self._submit_worker,
            args=(safe_request, encoded, api_key),
            name="debug-surgery-record-submit",
            daemon=True,
        )
        thread.start()
        return request_id

    def clear_history(self) -> None:
        with self._lock:
            if self._state == "SUBMITTING":
                raise ValueError("cannot clear history while a request is in progress")
            self._history.clear()
            self._last_result = {}
            self._last_error = ""
            self._state = "IDLE"

    def drain_events(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._events)
            self._events.clear()
        return rows

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "active_request_id": self._active_request_id,
                "default_endpoint": self._default_endpoint,
                "input_dir": str(self._input_dir),
                "examples": list(self._cases),
                "last_error": self._last_error,
                "last_result": dict(self._last_result),
                "history": list(self._history),
                "api_key_configured": self._api_key_configured(),
                "contract": {
                    "method": "POST",
                    "content_type": "application/json; charset=utf-8",
                    "auth_header": "X-API-Key",
                    "max_text_characters": MAX_TEXT_CHARS,
                    "max_body_bytes": MAX_BODY_BYTES,
                    "max_response_bytes": MAX_RESPONSE_BYTES,
                    "max_response_text_bytes": MAX_RESPONSE_TEXT_BYTES,
                    "server_timeout_sec": self._timeout_sec,
                    "generated_record_body_returned": False,
                    "result_lookup_defined": False,
                    "auto_retry": False,
                    "reconciliation_defined": False,
                    "allowed_endpoints": list(self._allowed_endpoints),
                },
            }

    def _read_case(self, case_id: str) -> tuple[str, str]:
        if not re.fullmatch(r"0704_(?:[6-9]|1[0-7])", case_id):
            raise ValueError("case_id must be one of 0704_6 through 0704_17")
        filename = f"{case_id}_surgery_record.txt"
        path = (self._input_dir / filename).resolve()
        root = self._input_dir.resolve()
        if path.parent != root or not path.is_file():
            raise ValueError(f"example TXT is unavailable: {filename}")
        return path.read_text(encoding="utf-8"), filename

    @staticmethod
    def _case_sort_key(name: str) -> tuple[int, str]:
        match = re.match(r"0704_(\d+)", name)
        return (int(match.group(1)) if match else 999, name)

    def _submit_worker(
        self, safe_request: dict[str, Any], encoded: bytes, api_key: str
    ) -> None:
        started = time.monotonic()
        secrets = (api_key,)
        request: Request | None = None
        http_status = 0
        response_headers: dict[str, str] = {}
        response_json: dict[str, Any] | None = None
        response_text = ""
        transport_error = ""
        response_error = ""
        remote_state_unknown = False
        try:
            request = Request(
                str(safe_request["endpoint"]),
                data=encoded,
                method="POST",
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                    "X-API-Key": api_key,
                    "User-Agent": "Taskplanner-Integration-Debug/1.0",
                },
            )
            with self._opener(request, timeout=self._timeout_sec) as response:
                status = getattr(response, "status", None)
                http_status = int(status if status is not None else response.getcode())
                response_headers = _response_headers(response, secrets=secrets)
                response_json, response_text = _decode_response(
                    _read_response(response), secrets=secrets
                )
        except HTTPError as exc:
            http_status = int(exc.code)
            response_headers = _response_headers(exc, secrets=secrets)
            try:
                response_json, response_text = _decode_response(
                    _read_response(exc), secrets=secrets
                )
            except ResponseTooLargeError as response_exc:
                response_error = str(response_exc)
            finally:
                exc.close()
        except ResponseTooLargeError as exc:
            response_error = str(exc)
        except (TimeoutError, URLError, OSError) as exc:
            remote_state_unknown = _is_timeout_error(exc)
            transport_error = _redact_text(f"{type(exc).__name__}: {exc}", secrets)
        except Exception as exc:
            transport_error = _redact_text(
                f"{type(exc).__name__}: {exc}", secrets
            )
        finally:
            # Release the only runtime-owned objects containing the credential as
            # soon as the network attempt finishes. Persistent state uses only
            # safe_request and redacted response values.
            request = None
            secrets = ()
            api_key = ""
        duration = round(max(0.0, time.monotonic() - started), 3)
        data = response_json.get("data", {}) if isinstance(response_json, dict) else {}
        valid_receipt = (
            isinstance(data, dict)
            and isinstance(data.get("id"), str)
            and bool(data["id"].strip())
            and isinstance(data.get("receivedAt"), str)
            and bool(data["receivedAt"].strip())
        )
        success = (
            not response_error
            and http_status == 201
            and isinstance(response_json, dict)
            and response_json.get("result") == "success"
            and valid_receipt
        )
        if not response_error and http_status == 201 and not success:
            response_error = "201 response does not match the documented receipt schema"
        error = (
            response_json.get("error", {})
            if isinstance(response_json, dict)
            else {}
        )
        state = (
            "SUCCEEDED"
            if success
            else "REMOTE_STATE_UNKNOWN"
            if remote_state_unknown
            else "FAILED"
        )
        result = {
            **safe_request,
            "completed_at": _utc_now(),
            "duration_sec": duration,
            "http_status": http_status,
            "success": success,
            "state": state,
            "transport_error": transport_error,
            "response_error": response_error,
            "response_headers": response_headers,
            "response_json": response_json,
            "response_text": response_text if response_json is None else "",
            "receipt_id": str(data.get("id", "")) if isinstance(data, dict) else "",
            "received_at": (
                str(data.get("receivedAt", "")) if isinstance(data, dict) else ""
            ),
            "error_code": (
                "RESPONSE_TOO_LARGE"
                if response_error.startswith("response body exceeds")
                else "INVALID_RESPONSE"
                if response_error
                else str(error.get("code", ""))
                if isinstance(error, dict)
                else ""
            ),
            "error_message": (
                str(error.get("message", "")) if isinstance(error, dict) else ""
            ),
            "generated_record_body_returned": bool(
                isinstance(data, dict)
                and any(
                    key in data
                    for key in ("summary", "note", "record", "report", "text")
                )
            ),
        }
        last_error = ""
        if not success:
            last_error = (
                transport_error
                or response_error
                or result["error_message"]
                or f"API returned HTTP {http_status or 'no response'}"
            )
        with self._lock:
            self._state = state
            self._active_request_id = ""
            self._last_result = result
            self._last_error = last_error
            self._history.append(result)
            self._events.append(
                {
                    "type": "record_submit_finished",
                    "request_id": safe_request["request_id"],
                    "case_id": safe_request["case_id"],
                    "http_status": http_status,
                    "success": success,
                    "state": state,
                    "receipt_id": result["receipt_id"],
                    "duration_sec": duration,
                    "error_code": result["error_code"],
                    "transport_error": transport_error,
                }
            )
