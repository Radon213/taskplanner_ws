import io
import json
from email.message import Message
import socket
import time
from urllib.error import HTTPError, URLError

import pytest

from integration_debug.surgery_record_runtime import (
    MAX_API_KEY_FILE_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_RESPONSE_TEXT_BYTES,
    MAX_TEXT_CHARS,
    REDACTED,
    SurgeryRecordRuntime,
    validate_endpoint,
    validate_request_fields,
)


ENDPOINT = "https://records.example.test/api/v1/surgery/img_texts"
SECRET = "test-only-api-key-never-persist"


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {"Content-Type": "application/json"}
        self.read_amounts: list[int] = []

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, amount: int = -1) -> bytes:
        self.read_amounts.append(amount)
        return self._body if amount < 0 else self._body[:amount]


def _runtime(tmp_path, opener) -> SurgeryRecordRuntime:
    secret_file = tmp_path / "puzzle-surgery-record-api-key"
    secret_file.write_text(SECRET + "\n", encoding="utf-8")
    secret_file.chmod(0o600)
    return SurgeryRecordRuntime(
        input_dir=tmp_path,
        default_endpoint=ENDPOINT,
        api_key_file=secret_file,
        timeout_sec=1,
        opener=opener,
    )


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "room_name": "OR03",
        "surgery_code": "0704_6",
        "date": "2026-07-04",
        "text": "surgery record input",
    }
    payload.update(overrides)
    return payload


def _wait(runtime: SurgeryRecordRuntime) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        snapshot = runtime.snapshot()
        if snapshot["state"] != "SUBMITTING":
            return snapshot
        time.sleep(0.005)
    pytest.fail("surgery-record worker did not finish")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def test_scans_only_0704_6_through_0704_17_in_numeric_order(tmp_path) -> None:
    for number in range(6, 18):
        (tmp_path / f"0704_{number}_surgery_record.txt").write_text(
            f"Case: 0704_{number}\n[00:00:00.000] Phase | start\n",
            encoding="utf-8",
        )
    (tmp_path / "0704_5_surgery_record.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "0704_18_surgery_record.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")

    runtime = _runtime(tmp_path, lambda *_args, **_kwargs: pytest.fail("network used"))
    rows = runtime.snapshot()["examples"]

    assert [row["case_id"] for row in rows] == [f"0704_{n}" for n in range(6, 18)]
    assert all(row["valid_for_api"] for row in rows)
    assert all(len(row["sha256"]) == 64 for row in rows)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://records.example.test/api/v1/surgery/img_texts",
        "https://user:password@records.example.test/api/v1/surgery/img_texts",
        "https://records.example.test/",
        "https://records.example.test/api/v1/surgery/img_texts?token=value",
        "https://records.example.test/api/v1/surgery/img_texts#fragment",
    ],
)
def test_endpoint_validation_rejects_unsafe_or_incomplete_urls(endpoint) -> None:
    with pytest.raises(ValueError):
        validate_endpoint(endpoint)


def test_submit_rejects_endpoint_outside_runtime_allowlist(tmp_path) -> None:
    runtime = _runtime(tmp_path, lambda *_args, **_kwargs: pytest.fail("network used"))
    with pytest.raises(ValueError, match="allowlist"):
        runtime.submit_async(
            _payload(endpoint="https://attacker.example/api/v1/collect")
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"room_name": None}, "roomName must be a string"),
        ({"room_name": "R" * 101}, "at most 100"),
        ({"surgery_code": "bad code"}, "letters, numbers"),
        ({"date": "20260704"}, "YYYY-MM-DD"),
        ({"date": "2026-02-30"}, "YYYY-MM-DD"),
        ({"text": "   "}, "text is required"),
        ({"text": "x" * (MAX_TEXT_CHARS + 1)}, "at most 65,535"),
    ],
)
def test_request_field_validation_is_strict(overrides, message) -> None:
    values = {
        "room_name": "OR03",
        "surgery_code": "0704_6",
        "surgery_date": "2026-07-04",
        "text": "record",
    }
    if "date" in overrides:
        overrides = {**overrides, "surgery_date": overrides["date"]}
        del overrides["date"]
    values.update(overrides)
    with pytest.raises(ValueError, match=message):
        validate_request_fields(**values)


def test_api_key_must_come_from_server_file_not_browser_or_environment(
    tmp_path, monkeypatch
) -> None:
    called = False

    def opener(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be attempted")

    monkeypatch.setenv("PUZZLE_SURGERY_RECORD_API_KEY", SECRET)
    runtime = _runtime(tmp_path, opener)

    snapshot = runtime.snapshot()
    assert snapshot["api_key_configured"] is True
    serialized_snapshot = json.dumps(snapshot, ensure_ascii=False)
    assert SECRET not in serialized_snapshot
    assert "api_key_file" not in serialized_snapshot

    with pytest.raises(ValueError, match="must not be supplied by the browser"):
        runtime.submit_async(_payload(api_key="browser-secret"))
    assert not called
    assert SECRET not in json.dumps(runtime.snapshot(), ensure_ascii=False)


def test_missing_server_api_key_is_reported_without_path_or_secret(tmp_path) -> None:
    runtime = SurgeryRecordRuntime(
        input_dir=tmp_path,
        default_endpoint=ENDPOINT,
        api_key_file=tmp_path / "missing-secret",
        timeout_sec=1,
        opener=lambda *_args, **_kwargs: pytest.fail("network used"),
    )

    assert runtime.snapshot()["api_key_configured"] is False
    with pytest.raises(ValueError, match="server X-API-Key is not configured"):
        runtime.submit_async(_payload())


@pytest.mark.parametrize(
    "configure_secret",
    [
        lambda path: path.write_text(SECRET, encoding="utf-8"),
        lambda path: (path.write_text(SECRET, encoding="utf-8"), path.chmod(0o644)),
        lambda path: (
            path.write_text("x" * (MAX_API_KEY_FILE_BYTES + 1), encoding="utf-8"),
            path.chmod(0o600),
        ),
        lambda path: (
            path.write_text(f"{SECRET}\nsecond-line\n", encoding="utf-8"),
            path.chmod(0o600),
        ),
    ],
)
def test_server_api_key_file_fails_closed_when_not_secure(
    tmp_path, configure_secret
) -> None:
    secret_file = tmp_path / "bad-secret"
    configure_secret(secret_file)
    runtime = SurgeryRecordRuntime(
        input_dir=tmp_path,
        default_endpoint=ENDPOINT,
        api_key_file=secret_file,
        timeout_sec=1,
        opener=lambda *_args, **_kwargs: pytest.fail("network used"),
    )

    assert runtime.snapshot()["api_key_configured"] is False
    with pytest.raises(ValueError, match="server X-API-Key"):
        runtime.submit_async(_payload())


def test_201_posts_json_and_persists_only_redacted_response(tmp_path) -> None:
    seen: dict[str, object] = {}
    response = FakeResponse(
        201,
        _json_bytes(
            {
                "result": "success",
                "data": {
                    "id": "txt_01J4X8Z3K9",
                    "receivedAt": "2026-08-03T10:15:30+09:00",
                    "api_key": SECRET,
                    "nested": {
                        "accessToken": "server-token",
                        "message": f"server reflected {SECRET}",
                    },
                },
            }
        ),
        {"Content-Type": "application/json", "X-Request-Id": "request-7"},
    )

    def opener(request, *, timeout):
        seen["method"] = request.get_method()
        seen["url"] = request.full_url
        seen["content_type"] = request.get_header("Content-type")
        seen["api_key"] = request.get_header("X-api-key")
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        return response

    runtime = _runtime(tmp_path, opener)
    runtime.submit_async(_payload())
    snapshot = _wait(runtime)
    events = runtime.drain_events()

    assert seen == {
        "method": "POST",
        "url": ENDPOINT,
        "content_type": "application/json; charset=utf-8",
        "api_key": SECRET,
        "body": {
            "roomName": "OR03",
            "surgeryCode": "0704_6",
            "date": "2026-07-04",
            "text": "surgery record input",
        },
        "timeout": 1.0,
    }
    assert response.read_amounts == [MAX_RESPONSE_BYTES + 1]
    assert snapshot["state"] == "SUCCEEDED"
    assert snapshot["last_result"]["receipt_id"] == "txt_01J4X8Z3K9"
    safe_response = snapshot["last_result"]["response_json"]
    assert safe_response["data"]["api_key"] == REDACTED
    assert safe_response["data"]["nested"]["accessToken"] == REDACTED
    assert safe_response["data"]["nested"]["message"] == f"server reflected {REDACTED}"
    assert SECRET not in json.dumps(snapshot, ensure_ascii=False)
    assert SECRET not in json.dumps(events, ensure_ascii=False)


def test_http_error_preserves_safe_contract_fields_and_redacts_secrets(
    tmp_path,
) -> None:
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Retry-After"] = "30"
    body = _json_bytes(
        {
            "result": "error",
            "error": {
                "code": "RATE_LIMITED",
                "message": f"retry without {SECRET}",
                "auth": SECRET,
            },
        }
    )

    def opener(request, *, timeout):
        raise HTTPError(
            request.full_url, 429, "rate limited", headers, io.BytesIO(body)
        )

    runtime = _runtime(tmp_path, opener)
    runtime.submit_async(_payload())
    snapshot = _wait(runtime)

    assert snapshot["state"] == "FAILED"
    result = snapshot["last_result"]
    assert result["http_status"] == 429
    assert result["error_code"] == "RATE_LIMITED"
    assert result["response_headers"]["retry-after"] == "30"
    assert result["response_json"]["error"]["auth"] == REDACTED
    assert SECRET not in json.dumps(snapshot, ensure_ascii=False)


def test_non_timeout_transport_error_is_failed(tmp_path) -> None:
    def opener(*_args, **_kwargs):
        raise URLError(ConnectionRefusedError("connection refused"))

    runtime = _runtime(tmp_path, opener)
    runtime.submit_async(_payload())
    snapshot = _wait(runtime)

    assert snapshot["state"] == "FAILED"
    assert snapshot["last_result"]["http_status"] == 0
    assert "connection refused" in snapshot["last_error"]


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timed out"),
        socket.timeout("timed out"),
        URLError(socket.timeout("timed out")),
        URLError("operation timed out"),
    ],
)
def test_timeout_leaves_remote_state_unknown_and_disables_retry(
    tmp_path, failure
) -> None:
    def opener(*_args, **_kwargs):
        raise failure

    runtime = _runtime(tmp_path, opener)
    runtime.submit_async(_payload())
    snapshot = _wait(runtime)
    events = runtime.drain_events()

    assert snapshot["state"] == "REMOTE_STATE_UNKNOWN"
    assert snapshot["last_result"]["state"] == "REMOTE_STATE_UNKNOWN"
    assert snapshot["contract"]["auto_retry"] is False
    assert snapshot["contract"]["reconciliation_defined"] is False
    assert events[-1]["state"] == "REMOTE_STATE_UNKNOWN"


def test_response_body_is_bounded_and_oversize_fails(tmp_path) -> None:
    response = FakeResponse(201, b"x" * (MAX_RESPONSE_BYTES + 1))
    runtime = _runtime(tmp_path, lambda *_args, **_kwargs: response)

    runtime.submit_async(_payload())
    snapshot = _wait(runtime)

    assert response.read_amounts == [MAX_RESPONSE_BYTES + 1]
    assert snapshot["state"] == "FAILED"
    assert snapshot["last_result"]["error_code"] == "RESPONSE_TOO_LARGE"
    assert snapshot["last_result"]["response_text"] == ""


def test_non_json_response_text_is_redacted_and_limited_to_16_kib(tmp_path) -> None:
    response = FakeResponse(500, (f"echo={SECRET}\n" + "x" * 20_000).encode())
    runtime = _runtime(tmp_path, lambda *_args, **_kwargs: response)

    runtime.submit_async(_payload())
    snapshot = _wait(runtime)
    response_text = snapshot["last_result"]["response_text"]

    assert snapshot["state"] == "FAILED"
    assert SECRET not in response_text
    assert len(response_text.encode("utf-8")) <= MAX_RESPONSE_TEXT_BYTES


def test_non_json_response_redacts_secret_crossing_text_limit(tmp_path) -> None:
    prefix = "x" * (MAX_RESPONSE_TEXT_BYTES - 4)
    response = FakeResponse(500, f"{prefix}{SECRET}\ntrailer".encode())
    runtime = _runtime(tmp_path, lambda *_args, **_kwargs: response)

    runtime.submit_async(_payload())
    snapshot = _wait(runtime)
    response_text = snapshot["last_result"]["response_text"]

    assert snapshot["state"] == "FAILED"
    assert SECRET not in response_text
    assert all(SECRET[:length] not in response_text for length in range(4, len(SECRET)))
    assert len(response_text.encode("utf-8")) <= MAX_RESPONSE_TEXT_BYTES
