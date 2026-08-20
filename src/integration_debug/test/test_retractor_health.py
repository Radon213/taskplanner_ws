from __future__ import annotations

from types import SimpleNamespace
import threading

import pytest

from procedure_spec import (
    NormalizedRetractionCommand,
    RetractionCommand,
    RetractionTargetSide,
)

from integration_debug.retractor_health import (
    FixedVLMRuntimeClient,
    VLMRuntimeStatus,
    run_retraction_vlm_health_probe,
)
from integration_debug.node import IntegrationDebugNode


def _interpretation(
    *,
    command: RetractionCommand | None,
    source: str,
    invoked: bool,
    detail: str,
):
    return SimpleNamespace(
        normalized=NormalizedRetractionCommand(
            command=command,
            target_side=RetractionTargetSide.NONE,
            distance_m=0.0,
            confidence=0.8 if command else 0.0,
            reason="probe",
        ),
        interpreter_source=source,
        vlm_invoked=invoked,
        detail=detail,
    )


def test_health_probe_requires_the_actual_expected_model_interpretation() -> None:
    class Interpreter:
        @staticmethod
        def interpret(transcript, state):
            assert transcript == "리트렉터 직접 가르치기 모드 켜줘"
            assert state.value == "idle"
            return _interpretation(
                command=RetractionCommand.START_DIRECT_TEACH,
                source="text_vlm",
                invoked=True,
                detail="text_vlm_normalized",
            )

    ticks = iter((10.0, 10.012))
    result = run_retraction_vlm_health_probe(
        Interpreter(), monotonic=lambda: next(ticks)
    )

    assert result.healthy is True
    assert result.latency_ms == pytest.approx(12.0)
    assert result.as_dict()["micro_test_passed"] is True
    assert result.as_dict()["actual_command"] == "start_direct_teach"


def test_deterministic_fallback_does_not_claim_model_worker_health() -> None:
    interpreter = SimpleNamespace(
        interpret=lambda *_args: _interpretation(
            command=RetractionCommand.START_DIRECT_TEACH,
            source="deterministic_fallback",
            invoked=True,
            detail="text_vlm_unavailable:URLError",
        )
    )

    result = run_retraction_vlm_health_probe(interpreter)

    assert result.healthy is False
    assert result.actual_command == "start_direct_teach"
    assert result.interpreter_source == "deterministic_fallback"


def test_health_probe_contains_interpreter_exception() -> None:
    def fail(*_args):
        raise RuntimeError("worker exploded")

    result = run_retraction_vlm_health_probe(
        SimpleNamespace(interpret=fail)
    )

    assert result.healthy is False
    assert result.error_type == "RuntimeError"
    assert result.detail == "retraction_vlm_health_probe_error:RuntimeError"


def test_fixed_runtime_refresh_observes_only_the_configured_catalog_row() -> None:
    calls = []

    def request(method, url, payload, timeout, headers):
        calls.append((method, url, payload, timeout, headers))
        if url.endswith("/health"):
            return {"ready": True}
        return {
            "data": [
                {
                    "id": "other-model",
                    "load_state": "loaded",
                    "loaded": True,
                    "available": True,
                    "runtime_managed": True,
                },
                {
                    "id": "fixed-model",
                    "load_state": "unloaded",
                    "loaded": False,
                    "available": True,
                    "runtime_managed": True,
                    "detail": "ready to load",
                },
            ]
        }

    client = FixedVLMRuntimeClient(
        base_url="http://127.0.0.1:8080",
        model_id="fixed-model",
        api_key="secret-key",
        request_json=request,
    )

    status = client.refresh()

    assert status.manager_reachable is True
    assert status.catalog_reachable is True
    assert status.load_state == "unloaded"
    assert status.runtime_managed is True
    assert status.available is True
    assert all(call[4]["Authorization"] == "Bearer secret-key" for call in calls)
    assert "secret-key" not in str(status.as_dict())


def test_explicit_load_posts_only_fixed_model_after_catalog_validation() -> None:
    calls = []

    def request(method, url, payload, _timeout, _headers):
        calls.append((method, url, payload))
        if url.endswith("/health"):
            return {"ready": True}
        if url.endswith("/v1/models"):
            return {
                "data": [
                    {
                        "id": "fixed-model",
                        "load_state": "unloaded",
                        "loaded": False,
                        "available": True,
                        "runtime_managed": True,
                    }
                ]
            }
        assert url.endswith("/manager/load")
        return {
            "model_id": "fixed-model",
            "state": "loading",
            "detail": "Loading fixed model",
        }

    status = FixedVLMRuntimeClient(
        base_url="http://127.0.0.1:8080",
        model_id="fixed-model",
        request_json=request,
    ).load()

    assert status.load_state == "loading"
    assert calls[-1] == (
        "POST",
        "http://127.0.0.1:8080/manager/load",
        {"model_id": "fixed-model"},
    )


def test_load_is_idempotent_and_refuses_unmanaged_or_unavailable_rows() -> None:
    for row in (
        {
            "load_state": "loaded",
            "loaded": True,
            "available": True,
            "runtime_managed": True,
        },
        {
            "load_state": "unloaded",
            "loaded": False,
            "available": False,
            "runtime_managed": True,
        },
        {
            "load_state": "unloaded",
            "loaded": False,
            "available": True,
            "runtime_managed": False,
        },
    ):
        posts = []

        def request(method, url, payload, _timeout, _headers):
            if method == "POST":
                posts.append((url, payload))
            if url.endswith("/health"):
                return {"ready": True}
            return {"data": [{"id": "fixed-model", **row}]}

        status = FixedVLMRuntimeClient(
            base_url="http://127.0.0.1:8080",
            model_id="fixed-model",
            request_json=request,
        ).load()

        assert posts == []
        if row["loaded"]:
            assert status.loaded is True
        else:
            assert status.detail == "configured_model_is_not_loadable"


def _node_harness():
    class Harness:
        pass

    harness = Harness()
    harness._lock = threading.RLock()
    harness._retraction_voice_vlm_base_url = "http://127.0.0.1:8080"
    harness._retraction_voice_vlm_model_id = "fixed-model"
    harness._vlm_status = {
        "last_probe_monotonic": 0.0,
        "micro_test": {},
    }
    harness._pending_vlm_observation = None
    harness._last_vlm_observation_submitted_monotonic = 1.0
    harness._apply_vlm_observation = (
        IntegrationDebugNode._apply_vlm_observation.__get__(harness)
    )
    harness._vlm_status_snapshot = (
        IntegrationDebugNode._vlm_status_snapshot.__get__(harness)
    )
    harness._retraction_interpretation = (
        IntegrationDebugNode._retraction_interpretation
    )
    return harness


def test_vlm_load_operation_uses_only_launch_fixed_runtime_identity() -> None:
    harness = _node_harness()
    calls = []

    class Runtime:
        @staticmethod
        def load():
            calls.append("load")
            return VLMRuntimeStatus(
                manager_reachable=True,
                catalog_reachable=True,
                load_state="loading",
                loaded=False,
                available=True,
                runtime_managed=True,
                detail="Loading fixed model",
            )

    harness._vlm_runtime = Runtime()

    accepted, _command_id, _message, status = (
        IntegrationDebugNode._handle_vlm_command(
            harness,
            "vlm_load",
            {
                "base_url": "http://malicious.invalid",
                "model_id": "arbitrary-model",
            },
        )
    )

    assert accepted is True
    assert calls == ["load"]
    assert status["base_url"] == "http://127.0.0.1:8080"
    assert status["model_id"] == "fixed-model"
    assert status["load_state"] == "loading"


def test_vlm_interpret_operation_returns_diagnostics_without_dispatch() -> None:
    harness = _node_harness()
    calls = []

    class Interpreter:
        @staticmethod
        def interpret(text, state):
            calls.append((text, state.value))
            return _interpretation(
                command=RetractionCommand.START_DIRECT_TEACH,
                source="text_vlm",
                invoked=True,
                detail="text_vlm_normalized",
            )

    harness._retraction_voice_interpreter = Interpreter()

    accepted, command_id, message, result = (
        IntegrationDebugNode._handle_vlm_command(
            harness,
            "vlm_interpret",
            {"text": "리트렉터 직접 가르치기 모드 켜줘", "state": "idle"},
        )
    )

    assert accepted is True
    assert command_id == ""
    assert "without ROS dispatch" in message
    assert calls == [("리트렉터 직접 가르치기 모드 켜줘", "idle")]
    assert result["command"] == "start_direct_teach"
    assert result["interpreter_source"] == "text_vlm"
    assert result["state"] == "completed"
    assert result["latency_ms"] >= 0.0
    assert result["dispatch_performed"] is False


def test_periodic_runtime_refresh_never_calls_model_interpreter() -> None:
    harness = _node_harness()

    class Runtime:
        @staticmethod
        def refresh():
            return VLMRuntimeStatus(
                manager_reachable=True,
                catalog_reachable=True,
                load_state="loaded",
                loaded=True,
                available=True,
                runtime_managed=True,
                detail="worker loaded",
            )

    class NoAutomaticInference:
        @staticmethod
        def interpret(*_args):
            raise AssertionError("periodic refresh must not run model inference")

    harness._vlm_runtime = Runtime()
    harness._retraction_voice_interpreter = NoAutomaticInference()

    runtime, health = IntegrationDebugNode._observe_vlm_runtime(harness)

    assert runtime.loaded is True
    assert health is None
