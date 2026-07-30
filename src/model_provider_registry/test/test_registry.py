from __future__ import annotations

import json
import threading
import time

import requests

from model_provider_registry import (
    ConfiguredModel,
    ModelProviderRegistry,
    ProviderConfig,
)


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def wait_for_runtime_state(
    registry: ModelProviderRegistry,
    provider_id: str,
    model_id: str,
    expected_state: str,
) -> tuple[str, str]:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        state = registry.runtime_state(provider_id, model_id)
        if state is not None and state[0] == expected_state:
            return state
        time.sleep(0.01)
    raise AssertionError(
        f"{provider_id}/{model_id} did not reach {expected_state}"
    )


def test_parallel_catalog_keeps_provider_identity_and_partial_failure():
    active = 0
    max_active = 0
    lock = threading.Lock()
    seen_headers = {}

    def fake_get(url, *, headers, timeout):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            seen_headers[url] = dict(headers)
        time.sleep(0.04)
        try:
            if ":8001" in url:
                raise requests.ConnectionError("vLLM is offline")
            if ":8888" in url:
                return FakeResponse(
                    {
                        "data": [
                            {
                                "id": "shared-model",
                                "display_name": "Shared Model",
                                "loaded": True,
                                "modalities": ["text", "image"],
                            }
                        ]
                    }
                )
            return FakeResponse({"data": [{"id": "shared-model", "owned_by": "lmstudio"}]})
        finally:
            with lock:
                active -= 1

    registry = ModelProviderRegistry(
        [
            ProviderConfig("lmstudio", "LM Studio", "http://127.0.0.1:1234"),
            ProviderConfig("unsloth", "Unsloth Studio", "http://127.0.0.1:8888", "secret"),
            ProviderConfig("vllm", "vLLM", "http://127.0.0.1:8001"),
        ],
        timeout_sec=0.5,
        request_get=fake_get,
    )

    probes = registry.probe_all()

    assert max_active >= 2
    assert [probe.provider.provider_id for probe in probes] == ["lmstudio", "unsloth", "vllm"]
    assert [probe.status for probe in probes] == ["online", "online", "offline"]
    assert probes[0].models[0].model_id == probes[1].models[0].model_id
    assert probes[0].models[0].provider_id != probes[1].models[0].provider_id
    assert probes[1].models[0].capability == "vision"
    assert probes[1].models[0].load_state == "loaded"
    assert seen_headers["http://127.0.0.1:1234/api/v1/models"] == {}
    assert seen_headers["http://127.0.0.1:8888/v1/models"] == {
        "Authorization": "Bearer secret"
    }


def test_auth_failure_is_distinct_from_transport_failure():
    registry = ModelProviderRegistry(
        [ProviderConfig("unsloth", "Unsloth Studio", "http://127.0.0.1:8888", "wrong")],
        request_get=lambda *args, **kwargs: FakeResponse({}, status_code=401),
    )

    probe = registry.probe("unsloth")

    assert not probe.reachable
    assert probe.status == "auth_error"
    assert probe.models == ()


def test_auto_resolution_matches_legacy_endpoint():
    registry = ModelProviderRegistry(
        [
            ProviderConfig("lmstudio", "LM Studio", "http://127.0.0.1:1234"),
            ProviderConfig("unsloth", "Unsloth Studio", "http://127.0.0.1:8888", "key"),
        ]
    )

    selected = registry.resolve(
        "auto",
        fallback_base_url="http://127.0.0.1:8888/",
        fallback_api_key="legacy",
    )

    assert selected.provider_id == "unsloth"
    assert selected.api_key == "key"


def test_lmstudio_native_catalog_reports_loaded_and_unloaded_models():
    def fake_get(url, *, headers, timeout):
        assert url == "http://127.0.0.1:1234/api/v1/models"
        return FakeResponse(
            {
                "models": [
                    {
                        "key": "loaded-vlm",
                        "display_name": "Loaded VLM",
                        "type": "llm",
                        "loaded_instances": [{"id": "loaded-vlm"}],
                        "capabilities": {"vision": True},
                    },
                    {
                        "key": "idle-llm",
                        "display_name": "Idle LLM",
                        "type": "llm",
                        "loaded_instances": [],
                        "capabilities": {"vision": False},
                    },
                ]
            }
        )

    registry = ModelProviderRegistry(
        [
            ProviderConfig(
                "lmstudio",
                "LM Studio",
                "http://127.0.0.1:1234",
                managed=True,
            )
        ],
        request_get=fake_get,
    )

    probe = registry.probe("lmstudio")

    assert probe.reachable
    assert probe.detail == "2 model(s); 1 loaded"
    assert [model.load_state for model in probe.models] == ["loaded", "unloaded"]
    assert [model.capability for model in probe.models] == ["vision", "text"]
    assert probe.models[0].available_actions == ("unload",)
    assert probe.models[1].available_actions == ("load",)


def test_manager_state_is_not_collapsed_to_unloaded():
    registry = ModelProviderRegistry(
        [ProviderConfig("vllm", "vLLM", "http://127.0.0.1:8001")],
        request_get=lambda *args, **kwargs: FakeResponse(
            {
                "data": [
                    {
                        "id": "managed-model",
                        "loaded": False,
                        "load_state": "loading",
                    }
                ]
            }
        ),
    )

    probe = registry.probe("vllm")

    assert probe.models[0].load_state == "loading"


def test_managed_runtime_control_uses_provider_scoped_auth():
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(
            url=url,
            json=json,
            headers=headers,
            timeout=timeout,
        )
        return FakeResponse(
            {
                "model_id": "managed-model",
                "state": "loading",
                "detail": "Starting worker",
            },
            status_code=202,
        )

    registry = ModelProviderRegistry(
        [
            ProviderConfig(
                "vllm",
                "vLLM",
                "http://127.0.0.1:8001",
                "secret",
                managed=True,
            )
        ],
        timeout_sec=1.5,
        request_post=fake_post,
    )

    result = registry.control_runtime("vllm", "managed-model", "load")

    assert result.success
    assert result.state == "loading"
    assert captured == {
        "url": "http://127.0.0.1:8001/manager/load",
        "json": {"model_id": "managed-model"},
        "headers": {"Authorization": "Bearer secret"},
        "timeout": 1.5,
    }


def test_ninfer_manager_uses_same_explicit_runtime_control_contract():
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(
            url=url,
            json=json,
            headers=headers,
            timeout=timeout,
        )
        return FakeResponse(
            {
                "model_id": "qwen-vlm",
                "state": "loading",
                "detail": "Starting NInfer worker",
            },
            status_code=202,
        )

    registry = ModelProviderRegistry(
        [
            ProviderConfig(
                "ninfer",
                "NInfer",
                "http://127.0.0.1:8080",
                "secret",
                managed=True,
                manager_mode=True,
            )
        ],
        timeout_sec=1.5,
        request_post=fake_post,
    )

    result = registry.control_runtime("ninfer", "qwen-vlm", "load")

    assert result.success
    assert result.state == "loading"
    assert captured == {
        "url": "http://127.0.0.1:8080/manager/load",
        "json": {"model_id": "qwen-vlm"},
        "headers": {"Authorization": "Bearer secret"},
        "timeout": 1.5,
    }


def test_external_provider_rejects_lifecycle_control():
    registry = ModelProviderRegistry(
        [ProviderConfig("lmstudio", "LM Studio", "http://127.0.0.1:1234")]
    )

    result = registry.control_runtime("lmstudio", "model", "unload")

    assert not result.success
    assert result.state == "unsupported"


def test_lmstudio_native_load_runs_asynchronously():
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return FakeResponse(
            {
                "instance_id": "loaded-instance",
                "status": "loaded",
            }
        )

    registry = ModelProviderRegistry(
        [
            ProviderConfig(
                "lmstudio",
                "LM Studio",
                "http://127.0.0.1:1234",
                "lm-key",
                managed=True,
            )
        ],
        runtime_timeout_sec=12.0,
        request_post=fake_post,
    )

    result = registry.control_runtime("lmstudio", "vision-model", "load")

    assert result.success
    assert result.state == "loading"
    state = wait_for_runtime_state(
        registry,
        "lmstudio",
        "vision-model",
        "loaded",
    )
    assert "loaded-instance" in state[1]
    assert captured == {
        "url": "http://127.0.0.1:1234/api/v1/models/load",
        "json": {"model": "vision-model"},
        "headers": {"Authorization": "Bearer lm-key"},
        "timeout": 12.0,
    }


def test_lmstudio_native_unload_resolves_all_instance_ids():
    posts = []

    def fake_get(url, *, headers, timeout):
        assert url == "http://127.0.0.1:1234/api/v1/models"
        return FakeResponse(
            {
                "models": [
                    {
                        "key": "vision-model",
                        "loaded_instances": [
                            {"id": "instance-a"},
                            {"id": "instance-b"},
                        ],
                    }
                ]
            }
        )

    def fake_post(url, *, json, headers, timeout):
        posts.append((url, json))
        return FakeResponse({"instance_id": json["instance_id"]})

    registry = ModelProviderRegistry(
        [
            ProviderConfig(
                "lmstudio",
                "LM Studio",
                "http://127.0.0.1:1234",
                managed=True,
            )
        ],
        request_get=fake_get,
        request_post=fake_post,
    )

    result = registry.control_runtime("lmstudio", "vision-model", "unload")

    assert result.success
    assert result.state == "unloading"
    wait_for_runtime_state(registry, "lmstudio", "vision-model", "unloaded")
    assert posts == [
        (
            "http://127.0.0.1:1234/api/v1/models/unload",
            {"instance_id": "instance-a"},
        ),
        (
            "http://127.0.0.1:1234/api/v1/models/unload",
            {"instance_id": "instance-b"},
        ),
    ]


def test_unsloth_native_load_splits_configured_gguf_variant():
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(url=url, json=json, headers=headers)
        return FakeResponse(
            {
                "status": "loaded",
                "model": json["model_path"],
            }
        )

    registry = ModelProviderRegistry(
        [
            ProviderConfig(
                "unsloth",
                "Unsloth Studio",
                "http://127.0.0.1:8888",
                "unsloth-key",
                managed=True,
            )
        ],
        request_post=fake_post,
    )
    model_id = "unsloth/Qwen3.6-35B-A3B-MTP-GGUF:Q2_K_XL"

    result = registry.control_runtime("unsloth", model_id, "load")

    assert result.success
    assert result.state == "loading"
    wait_for_runtime_state(registry, "unsloth", model_id, "loaded")
    assert captured == {
        "url": "http://127.0.0.1:8888/v1/load",
        "json": {
            "model_path": "unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
            "gguf_variant": "Q2_K_XL",
        },
        "headers": {"Authorization": "Bearer unsloth-key"},
    }


def test_native_provider_does_not_offer_vllm_only_sleep_commands():
    registry = ModelProviderRegistry(
        [
            ProviderConfig(
                "lmstudio",
                "LM Studio",
                "http://127.0.0.1:1234",
                managed=True,
            ),
            ProviderConfig(
                "unsloth",
                "Unsloth Studio",
                "http://127.0.0.1:8888",
                managed=True,
            ),
        ]
    )

    assert registry.available_actions("lmstudio", "loaded") == ("unload",)
    assert registry.available_actions("unsloth", "unloaded") == ("load",)
    result = registry.control_runtime("unsloth", "model", "sleep")
    assert not result.success
    assert result.state == "unsupported"


def test_ninfer_advertised_model_is_resident_and_external():
    registry = ModelProviderRegistry(
        [ProviderConfig("ninfer", "NInfer", "http://127.0.0.1:8080")],
        request_get=lambda *args, **kwargs: FakeResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": "qwen3.6-35b-a3b",
                        "object": "model",
                        "owned_by": "ninfer",
                    }
                ],
            }
        ),
    )

    probe = registry.probe("ninfer")

    assert probe.reachable
    assert probe.models[0].model_id == "qwen3.6-35b-a3b"
    assert probe.models[0].load_state == "loaded"
    assert probe.models[0].runtime_managed is False
    assert probe.models[0].available_actions == ()


def test_configured_ninfer_catalog_is_visible_while_endpoint_is_offline(tmp_path):
    artifact = tmp_path / "qwen.ninfer"
    artifact.write_bytes(b"artifact")
    process_starts = []
    provider = ProviderConfig(
        "ninfer",
        "NInfer",
        "http://127.0.0.1:8080",
        managed=True,
        configured_models=(
            ConfiguredModel(
                model_id="qwen-vision",
                display_name="Qwen Vision",
                capability="vision",
                artifact_path=str(artifact),
                start_command=("ninfer-serve", "{artifact}", "--model-id", "{model_id}"),
            ),
        ),
    )
    registry = ModelProviderRegistry(
        [provider],
        request_get=lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.ConnectionError("offline")
        ),
        process_popen=lambda *args, **kwargs: process_starts.append(args),
    )

    probe = registry.probe("ninfer")

    assert not probe.reachable
    assert probe.status == "offline"
    assert len(probe.models) == 1
    model = probe.models[0]
    assert model.model_id == "qwen-vision"
    assert model.installed is True
    assert model.available is True
    assert model.load_state == "unloaded"
    assert model.runtime_managed is True
    assert model.available_actions == ("load",)
    assert process_starts == []


def test_missing_ninfer_artifact_is_reported_as_model_error(tmp_path):
    provider = ProviderConfig(
        "ninfer",
        "NInfer",
        "http://127.0.0.1:8080",
        managed=True,
        configured_models=(
            ConfiguredModel(
                model_id="missing-model",
                artifact_path=str(tmp_path / "missing.ninfer"),
                start_command=("ninfer-serve", "{artifact}"),
            ),
        ),
    )
    registry = ModelProviderRegistry(
        [provider],
        request_get=lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.ConnectionError("offline")
        ),
    )

    model = registry.probe("ninfer").models[0]

    assert model.installed is False
    assert model.available is False
    assert model.load_state == "error"
    assert model.selectable is False
    assert model.available_actions == ()
    assert "missing" in model.detail.lower()


def test_ninfer_load_and_unload_use_existing_runtime_control_path(tmp_path):
    artifact = tmp_path / "qwen.ninfer"
    artifact.write_bytes(b"artifact")
    runtime = {"loaded": False}
    captured = {}

    class FakeProcess:
        def poll(self):
            return None if runtime["loaded"] else 0

        def terminate(self):
            runtime["loaded"] = False

        def wait(self, timeout):
            return 0

        def kill(self):
            runtime["loaded"] = False

    def fake_get(url, *, headers, timeout):
        if not runtime["loaded"]:
            raise requests.ConnectionError("offline")
        return FakeResponse({"data": [{"id": "qwen-vision"}]})

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        runtime["loaded"] = True
        return FakeProcess()

    provider = ProviderConfig(
        "ninfer",
        "NInfer",
        "http://127.0.0.1:8080",
        managed=True,
        configured_models=(
            ConfiguredModel(
                model_id="qwen-vision",
                capability="vision",
                artifact_path=str(artifact),
                start_command=(
                    "ninfer-serve",
                    "{artifact}",
                    "--model-id={model_id}",
                ),
            ),
        ),
    )
    registry = ModelProviderRegistry(
        [provider],
        runtime_timeout_sec=1.0,
        request_get=fake_get,
        process_popen=fake_popen,
        sleep=lambda _seconds: None,
    )

    load_result = registry.control_runtime("ninfer", "qwen-vision", "load")

    assert load_result.success
    assert load_result.state == "loading"
    wait_for_runtime_state(registry, "ninfer", "qwen-vision", "loaded")
    assert captured["command"] == [
        "ninfer-serve",
        str(artifact),
        "--model-id=qwen-vision",
    ]
    assert registry.probe("ninfer").models[0].load_state == "loaded"

    unload_result = registry.control_runtime("ninfer", "qwen-vision", "unload")

    assert unload_result.success
    assert unload_result.state == "unloading"
    wait_for_runtime_state(registry, "ninfer", "qwen-vision", "unloaded")
    assert runtime["loaded"] is False


def test_lmstudio_openai_fallback_does_not_claim_native_lifecycle_support():
    def fake_get(url, *, headers, timeout):
        if url.endswith("/api/v1/models"):
            return FakeResponse({}, status_code=404)
        return FakeResponse({"data": [{"id": "loaded-only-model"}]})

    registry = ModelProviderRegistry(
        [
            ProviderConfig(
                "lmstudio",
                "LM Studio",
                "http://127.0.0.1:1234",
                managed=True,
            )
        ],
        request_get=fake_get,
    )

    probe = registry.probe("lmstudio")
    model = probe.models[0]
    supported, detail = registry.runtime_capability("lmstudio")

    assert probe.reachable
    assert model.runtime_managed is False
    assert model.available_actions == ()
    assert supported is False
    assert "does not expose" in detail
    result = registry.control_runtime("lmstudio", model.model_id, "unload")
    assert not result.success
    assert result.state == "unsupported"


def test_external_lifecycle_404_becomes_persistent_capability_error():
    registry = ModelProviderRegistry(
        [
            ProviderConfig(
                "unsloth",
                "Unsloth Studio",
                "http://127.0.0.1:8888",
                managed=True,
            )
        ],
        request_post=lambda *args, **kwargs: FakeResponse(
            {"detail": "route not found"},
            status_code=404,
        ),
    )

    first = registry.control_runtime("unsloth", "model", "load")

    assert first.success
    assert first.state == "loading"
    state = wait_for_runtime_state(registry, "unsloth", "model", "error")
    assert "does not expose" in state[1]
    supported, detail = registry.runtime_capability("unsloth")
    assert supported is False
    assert "does not expose" in detail

    second = registry.control_runtime("unsloth", "model", "load")
    assert not second.success
    assert second.state == "unsupported"


def test_environment_ninfer_catalog_does_not_autoload(monkeypatch, tmp_path):
    artifact = tmp_path / "configured.ninfer"
    artifact.write_bytes(b"artifact")
    monkeypatch.setenv(
        "NINFER_MODEL_CATALOG_JSON",
        json.dumps(
            {
                "models": [
                    {
                        "id": "configured-ninfer",
                        "display_name": "Configured NInfer",
                        "capability": "vision",
                        "artifact_path": str(artifact),
                        "start_command": [
                            "ninfer-serve",
                            "{artifact}",
                            "--model-id",
                            "{model_id}",
                        ],
                    }
                ]
            }
        ),
    )
    monkeypatch.delenv("NINFER_PROVIDER_MANAGED", raising=False)
    process_starts = []
    registry = ModelProviderRegistry.from_environment(
        request_get=lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.ConnectionError("offline")
        ),
        process_popen=lambda *args, **kwargs: process_starts.append(args),
    )

    provider = registry.get_provider("ninfer")
    probe = registry.probe("ninfer")

    assert provider is not None
    assert provider.managed is True
    assert probe.models[0].model_id == "configured-ninfer"
    assert probe.models[0].load_state == "unloaded"
    assert process_starts == []


def test_all_four_provider_catalogs_are_combined_without_autoload(tmp_path):
    artifact = tmp_path / "ninfer.ninfer"
    artifact.write_bytes(b"artifact")

    def fake_get(url, *, headers, timeout):
        if ":1234" in url:
            return FakeResponse(
                {
                    "models": [
                        {
                            "key": "lmstudio-vlm",
                            "loaded_instances": [],
                            "capabilities": {"vision": True},
                        }
                    ]
                }
            )
        if ":8888" in url:
            return FakeResponse(
                {"data": [{"id": "unsloth-llm", "loaded": True}]}
            )
        if ":8001" in url:
            return FakeResponse(
                {
                    "data": [
                        {
                            "id": "vllm-vlm",
                            "installed": True,
                            "available": True,
                            "load_state": "unloaded",
                        }
                    ]
                }
            )
        raise requests.ConnectionError("NInfer endpoint is intentionally offline")

    registry = ModelProviderRegistry(
        [
            ProviderConfig(
                "lmstudio",
                "LM Studio",
                "http://127.0.0.1:1234",
                managed=True,
            ),
            ProviderConfig(
                "unsloth",
                "Unsloth Studio",
                "http://127.0.0.1:8888",
                managed=True,
            ),
            ProviderConfig(
                "vllm",
                "vLLM",
                "http://127.0.0.1:8001",
                managed=True,
            ),
            ProviderConfig(
                "ninfer",
                "NInfer",
                "http://127.0.0.1:8080",
                managed=True,
                configured_models=(
                    ConfiguredModel(
                        model_id="ninfer-vlm",
                        artifact_path=str(artifact),
                        start_command=("ninfer-serve", "{artifact}"),
                    ),
                ),
            ),
        ],
        request_get=fake_get,
    )

    probes = registry.probe_all()

    assert [probe.provider.provider_id for probe in probes] == [
        "lmstudio",
        "unsloth",
        "vllm",
        "ninfer",
    ]
    assert [probe.status for probe in probes] == [
        "online",
        "online",
        "online",
        "offline",
    ]
    assert [probe.models[0].load_state for probe in probes] == [
        "unloaded",
        "loaded",
        "unloaded",
        "unloaded",
    ]
    assert probes[2].models[0].installed is True
    assert probes[2].models[0].available is True
    assert probes[3].models[0].runtime_managed is True
    assert probes[3].models[0].available_actions == ("load",)
