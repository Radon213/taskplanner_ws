from __future__ import annotations

from types import SimpleNamespace

from model_provider_registry import (
    CatalogModel,
    ProviderConfig,
    ProviderProbe,
    RuntimeControlResult,
)
from vlm_node.real_vlm import RealVLMNode


class OfflineManagedRegistry:
    def __init__(self) -> None:
        self.provider = ProviderConfig(
            "ninfer",
            "NInfer",
            "http://127.0.0.1:8080",
            managed=True,
        )
        self.model = CatalogModel(
            provider_id="ninfer",
            provider_name="NInfer",
            model_id="qwen-vlm",
            display_name="Qwen VLM",
            capability="vision",
            load_state="unloaded",
            selectable=True,
            runtime_managed=True,
            available_actions=("load",),
            installed=True,
            available=True,
        )
        self.ensure_calls: list[tuple[str, str, str]] = []

    def get_provider(self, provider_id: str):
        return self.provider if provider_id == "ninfer" else None

    def probe(self, provider_id: str) -> ProviderProbe:
        assert provider_id == "ninfer"
        return ProviderProbe(
            provider=self.provider,
            reachable=False,
            status="offline",
            detail="endpoint is not running",
            latency_sec=0.01,
            models=(self.model,),
        )

    def matching_model(self, provider_id, model_id, models):
        del provider_id
        return next(
            (model for model in models if model.model_id == model_id),
            None,
        )

    def ensure_runtime_ready(
        self,
        provider_id,
        model,
        *,
        requested_model_id="",
    ) -> RuntimeControlResult:
        self.ensure_calls.append(
            (provider_id, model.model_id, requested_model_id)
        )
        return RuntimeControlResult(
            True,
            provider_id,
            requested_model_id,
            "loading",
            "NInfer is loading qwen-vlm",
        )


def test_offline_managed_model_selection_starts_runtime(monkeypatch):
    node = RealVLMNode.__new__(RealVLMNode)
    registry = OfflineManagedRegistry()
    node._provider_registry = registry
    node._provider_id = "vllm"
    node._model_id = "old-model"
    node._provider_model_selections = {}

    def set_parameters(_node, parameters):
        values = {parameter.name: parameter.value for parameter in parameters}
        node._provider_id = values["provider_id"]
        node._model_id = values["model_id"]
        return SimpleNamespace(successful=True, reason="")

    monkeypatch.setattr(
        RealVLMNode,
        "set_parameters_atomically",
        set_parameters,
    )
    request = SimpleNamespace(
        provider_id="ninfer",
        model_id="qwen-vlm",
    )
    response = SimpleNamespace(
        success=False,
        provider_id="",
        model_id="",
        message="",
    )

    result = node._on_select_model_provider(request, response)

    assert result.success
    assert result.provider_id == "ninfer"
    assert result.model_id == "qwen-vlm"
    assert "runtime loading" in result.message
    assert registry.ensure_calls == [
        ("ninfer", "qwen-vlm", "qwen-vlm")
    ]


def test_unreachable_external_provider_remains_rejected():
    node = RealVLMNode.__new__(RealVLMNode)
    registry = OfflineManagedRegistry()
    registry.provider = ProviderConfig(
        "ninfer",
        "NInfer",
        "http://127.0.0.1:8080",
        managed=False,
    )
    registry.model = CatalogModel(
        provider_id="ninfer",
        provider_name="NInfer",
        model_id="qwen-vlm",
        display_name="Qwen VLM",
        capability="vision",
        load_state="unloaded",
        selectable=False,
        runtime_managed=False,
        installed=True,
        available=False,
    )
    node._provider_registry = registry
    node._provider_id = "vllm"
    node._model_id = "old-model"
    node._provider_model_selections = {}
    request = SimpleNamespace(
        provider_id="ninfer",
        model_id="qwen-vlm",
    )
    response = SimpleNamespace(
        success=False,
        provider_id="",
        model_id="",
        message="",
    )

    result = node._on_select_model_provider(request, response)

    assert not result.success
    assert "unavailable" in result.message
    assert registry.ensure_calls == []
