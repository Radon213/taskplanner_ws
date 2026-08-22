from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_runtime_campaign",
    ROOT / "scripts" / "release_runtime_campaign.py",
)
assert SPEC is not None and SPEC.loader is not None
campaign = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = campaign
SPEC.loader.exec_module(campaign)


class FakeHeaders:
    def __init__(self, content_type: str) -> None:
        self.content_type = content_type

    def get(self, name: str, default: str = "") -> str:
        return self.content_type if name.lower() == "content-type" else default


class FakeResponse:
    def __init__(self, body: bytes, *, content_type: str, status: int = 200) -> None:
        self.body = body
        self.headers = FakeHeaders(content_type)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


def valid_web_responses() -> dict[str, FakeResponse]:
    runtime_status = {
        "phase": "idle",
        "active_mode": "llm-surgeon",
        "requested_mode": None,
        "message": "Selected runtime is ready.",
        "retryable": False,
    }
    return {
        "/": FakeResponse(b'<main><div id="root"></div></main>', content_type="text/html; charset=utf-8"),
        "/src/App.tsx": FakeResponse(b"const App = () => null;", content_type="text/javascript"),
        "/src/hooks/useRosBridge.ts": FakeResponse(b"export {};", content_type="text/javascript"),
        "/api/runtime/status": FakeResponse(
            json.dumps(runtime_status).encode("utf-8"),
            content_type="application/json",
        ),
    }


def install_web_responses(monkeypatch, responses: dict[str, FakeResponse]) -> None:
    def fake_urlopen(url: str, *, timeout: float):
        assert timeout == 2.0
        path = url.removeprefix(campaign.WEBAPP_BASE_URL)
        return responses[path]

    monkeypatch.setattr(campaign, "urlopen", fake_urlopen)


def test_web_readiness_validates_browser_and_runtime_contract(monkeypatch):
    install_web_responses(monkeypatch, valid_web_responses())

    assert campaign.web_readiness() == (True, "")
    assert campaign.web_ready() is True


def test_web_readiness_rejects_failed_critical_transform(monkeypatch):
    responses = valid_web_responses()
    responses["/src/App.tsx"] = FakeResponse(
        b"transform failed", content_type="text/plain", status=500
    )
    install_web_responses(monkeypatch, responses)

    assert campaign.web_readiness() == (False, "/src/App.tsx: HTTP 500")


def test_web_readiness_rejects_invalid_runtime_schema(monkeypatch):
    responses = valid_web_responses()
    responses["/api/runtime/status"] = FakeResponse(
        b'{"phase":"idle","retryable":"false"}',
        content_type="application/json",
    )
    install_web_responses(monkeypatch, responses)

    assert campaign.web_readiness() == (
        False,
        "/api/runtime/status: invalid runtime status schema",
    )


def test_web_readiness_rejects_non_scalar_runtime_mode(monkeypatch):
    responses = valid_web_responses()
    responses["/api/runtime/status"] = FakeResponse(
        b'{"phase":"idle","active_mode":[],"requested_mode":null,"retryable":false}',
        content_type="application/json",
    )
    install_web_responses(monkeypatch, responses)

    assert campaign.web_readiness() == (
        False,
        "/api/runtime/status: invalid runtime status schema",
    )


def test_web_readiness_rejects_wrong_root_content_type(monkeypatch):
    responses = valid_web_responses()
    responses["/"] = FakeResponse(
        b'<div id="root"></div>', content_type="application/json"
    )
    install_web_responses(monkeypatch, responses)

    assert campaign.web_readiness() == (
        False,
        "/: unexpected content type application/json",
    )


def test_parse_memory_bytes_supports_docker_units():
    assert campaign.parse_memory_bytes("512B / 1GiB") == 512
    assert campaign.parse_memory_bytes("1.5MiB / 31.25GiB") == 1572864
    assert campaign.parse_memory_bytes("2GB / 8GB") == 2_000_000_000


def test_memory_growth_uses_stable_windows_and_enforces_limit():
    samples = []
    for index, value in enumerate((100, 100, 101, 119, 120), start=1):
        samples.append(
            {
                "elapsed_sec": float(index * 10),
                "containers": [
                    {"container": "taskplanner-runtime", "memory_used_bytes": value}
                ],
            }
        )

    result = campaign.summarize_memory_growth(
        samples,
        warmup_sec=0.0,
        limit_percent=10.0,
    )

    assert result["status"] == "failed"
    assert result["violating_container_count"] == 1
    assert result["containers"][0]["growth_percent"] == 20.0


def test_memory_growth_reports_short_smoke_as_not_evaluated():
    result = campaign.summarize_memory_growth(
        [
            {
                "elapsed_sec": 1.0,
                "containers": [
                    {"container": "taskplanner-runtime", "memory_used_bytes": 100}
                ],
            }
        ],
        warmup_sec=0.0,
        limit_percent=10.0,
    )

    assert result["status"] == "not_evaluated"
    assert result["containers"][0]["status"] == "insufficient_samples"
