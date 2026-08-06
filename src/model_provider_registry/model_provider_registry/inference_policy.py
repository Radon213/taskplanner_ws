"""Provider-specific inference request policies."""

from __future__ import annotations

from typing import Any, MutableMapping


def force_disable_thinking(
    body: MutableMapping[str, Any],
    *,
    provider_id: str,
) -> None:
    """Disable reasoning generation using each provider's supported controls."""

    normalized_provider = provider_id.strip().lower()
    body["reasoning_effort"] = "none"

    if normalized_provider in {"lmstudio", "unsloth", "vllm"}:
        raw_kwargs = body.get("chat_template_kwargs")
        template_kwargs = dict(raw_kwargs) if isinstance(raw_kwargs, dict) else {}
        template_kwargs["enable_thinking"] = False
        body["chat_template_kwargs"] = template_kwargs

    if normalized_provider in {"unsloth", "ninfer"}:
        # Unsloth's plain enable_thinking templates do not treat effort=none
        # as authoritative. NInfer exposes the same typed extension and ignores
        # reasoning_effort, so both require the top-level switch.
        body["enable_thinking"] = False
