from typing import Any

import requests

from vlm_node.lmstudio_client import LMStudioClient


class _Response:
    elapsed = type("_Elapsed", (), {"total_seconds": lambda self: 0.01})()

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": "{}"}}]}


def test_unsloth_openai_request_forces_thinking_off(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(json)
        return _Response()

    monkeypatch.setattr(requests, "post", fake_post)
    client = LMStudioClient(
        base_url="http://127.0.0.1:8888",
        timeout_sec=1.0,
        provider_id="unsloth",
    )

    client.request_json(
        system_prompt="system",
        developer_prompt="developer",
        user_context_json="{}",
        images=[],
        model_id="model",
        temperature=0.0,
        top_p=1.0,
        max_output_tokens=32,
        api_mode="openai_compat",
        reasoning_effort="high",
    )

    assert captured["reasoning_effort"] == "none"
    assert captured["enable_thinking"] is False
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


def test_ninfer_request_uses_typed_thinking_switch_and_prompt_json(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(json)
        return _Response()

    monkeypatch.setattr(requests, "post", fake_post)
    client = LMStudioClient(
        base_url="http://127.0.0.1:8080",
        timeout_sec=1.0,
        provider_id="ninfer",
    )

    client.request_json(
        system_prompt="system",
        developer_prompt="developer",
        user_context_json="{}",
        images=[],
        model_id="qwen3.6-35b-a3b",
        temperature=0.0,
        top_p=1.0,
        max_output_tokens=32,
        api_mode="openai_compat",
        response_format="json_schema",
        json_schema={"type": "object"},
        reasoning_effort="high",
    )

    assert captured["reasoning_effort"] == "none"
    assert captured["enable_thinking"] is False
    assert "response_format" not in captured
    assert "chat_template_kwargs" not in captured


def test_ninfer_request_marks_only_the_stable_instruction_prefix_for_cache(
    monkeypatch,
):
    captured: dict[str, Any] = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(json)
        return _Response()

    monkeypatch.setattr(requests, "post", fake_post)
    client = LMStudioClient(
        base_url="http://127.0.0.1:8080",
        timeout_sec=1.0,
        provider_id="ninfer",
    )

    client.request_json(
        system_prompt="stable procedure",
        developer_prompt="retryable output contract",
        user_context_json='{"frame":1}',
        images=[],
        model_id="qwen3.6-35b-a3b",
        temperature=0.0,
        top_p=1.0,
        max_output_tokens=32,
        api_mode="openai_compat",
    )

    assert captured["prompt_cache_options"] == {"mode": "explicit"}
    system_content = captured["messages"][0]["content"]
    assert system_content == [
        {
            "type": "text",
            "text": "stable procedure",
            "prompt_cache_breakpoint": {"mode": "explicit"},
        },
        {"type": "text", "text": "retryable output contract"},
    ]


def test_non_ninfer_request_keeps_ordinary_system_message(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(json)
        return _Response()

    monkeypatch.setattr(requests, "post", fake_post)
    client = LMStudioClient(
        base_url="http://127.0.0.1:8001",
        timeout_sec=1.0,
        provider_id="vllm",
    )

    client.request_json(
        system_prompt="stable procedure",
        developer_prompt="output contract",
        user_context_json="{}",
        images=[],
        model_id="model",
        temperature=0.0,
        top_p=1.0,
        max_output_tokens=32,
        api_mode="openai_compat",
    )

    assert captured["messages"][0]["content"] == (
        "stable procedure\n\noutput contract"
    )
    assert "prompt_cache_options" not in captured


def test_lmstudio_openai_request_disables_template_thinking(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(json)
        return _Response()

    monkeypatch.setattr(requests, "post", fake_post)
    client = LMStudioClient(
        base_url="http://127.0.0.1:1234",
        timeout_sec=1.0,
        provider_id="lmstudio",
    )

    client.request_json(
        system_prompt="system",
        developer_prompt="developer",
        user_context_json="{}",
        images=[],
        model_id="qwen/qwen3.5-4b",
        temperature=0.0,
        top_p=1.0,
        max_output_tokens=32,
        api_mode="openai_compat",
        reasoning_effort="high",
    )

    assert captured["reasoning_effort"] == "none"
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}
    assert "enable_thinking" not in captured


def test_openai_request_includes_explicit_generation_seed(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(json)
        return _Response()

    monkeypatch.setattr(requests, "post", fake_post)
    client = LMStudioClient(
        base_url="http://127.0.0.1:8001",
        timeout_sec=1.0,
        provider_id="vllm",
    )

    client.request_json(
        system_prompt="system",
        developer_prompt="developer",
        user_context_json="{}",
        images=[],
        model_id="model",
        temperature=0.0,
        top_p=1.0,
        max_output_tokens=32,
        api_mode="openai_compat",
        generation_seed=0,
    )

    assert captured["seed"] == 0
