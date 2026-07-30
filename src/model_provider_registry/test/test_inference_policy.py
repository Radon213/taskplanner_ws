from model_provider_registry import force_disable_thinking


def test_unsloth_thinking_is_disabled_with_typed_and_template_controls():
    body = {
        "reasoning_effort": "high",
        "enable_thinking": True,
        "chat_template_kwargs": {
            "enable_thinking": True,
            "custom": "preserved",
        },
    }

    force_disable_thinking(body, provider_id="unsloth")

    assert body["reasoning_effort"] == "none"
    assert body["enable_thinking"] is False
    assert body["chat_template_kwargs"] == {
        "enable_thinking": False,
        "custom": "preserved",
    }


def test_vllm_thinking_is_disabled_at_chat_template_boundary():
    body = {}

    force_disable_thinking(body, provider_id="vllm")

    assert body["reasoning_effort"] == "none"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "enable_thinking" not in body


def test_lmstudio_uses_supported_reasoning_effort_without_unknown_fields():
    body = {}

    force_disable_thinking(body, provider_id="lmstudio")

    assert body == {"reasoning_effort": "none"}


def test_ninfer_uses_typed_thinking_switch():
    body = {}

    force_disable_thinking(body, provider_id="ninfer")

    assert body == {
        "reasoning_effort": "none",
        "enable_thinking": False,
    }
