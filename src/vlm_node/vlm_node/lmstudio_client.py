"""Small LM Studio REST client with native and OpenAI-compatible fallbacks."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(slots=True)
class LMStudioResponse:
    raw_text: str
    latency_sec: float
    mode: str


class LMStudioClient:
    def __init__(self, *, base_url: str, timeout_sec: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec

    def request_json(
        self,
        *,
        system_prompt: str,
        developer_prompt: str,
        user_context_json: str,
        images: list[tuple[str, bytes, str]],
        model_id: str,
        temperature: float,
        top_p: float,
        max_output_tokens: int,
        api_mode: str,
    ) -> LMStudioResponse:
        if api_mode == "openai_compat":
            return self._request_openai_compat(
                system_prompt=system_prompt,
                developer_prompt=developer_prompt,
                user_context_json=user_context_json,
                images=images,
                model_id=model_id,
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_output_tokens,
            )
        try:
            return self._request_native(
                system_prompt=system_prompt,
                developer_prompt=developer_prompt,
                user_context_json=user_context_json,
                images=images,
                model_id=model_id,
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_output_tokens,
            )
        except Exception:
            return self._request_openai_compat(
                system_prompt=system_prompt,
                developer_prompt=developer_prompt,
                user_context_json=user_context_json,
                images=images,
                model_id=model_id,
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_output_tokens,
            )

    def _request_native(
        self,
        *,
        system_prompt: str,
        developer_prompt: str,
        user_context_json: str,
        images: list[tuple[str, bytes, str]],
        model_id: str,
        temperature: float,
        top_p: float,
        max_output_tokens: int,
    ) -> LMStudioResponse:
        url = f"{self._base_url}/api/v1/chat"
        body = {
            "model": model_id,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_output_tokens,
            "messages": self._build_messages(
                system_prompt=system_prompt,
                developer_prompt=developer_prompt,
                user_context_json=user_context_json,
                images=images,
            ),
        }
        response = requests.post(url, json=body, timeout=self._timeout_sec)
        response.raise_for_status()
        raw = self._extract_text(response.json())
        return LMStudioResponse(raw_text=raw, latency_sec=response.elapsed.total_seconds(), mode="native")

    def _request_openai_compat(
        self,
        *,
        system_prompt: str,
        developer_prompt: str,
        user_context_json: str,
        images: list[tuple[str, bytes, str]],
        model_id: str,
        temperature: float,
        top_p: float,
        max_output_tokens: int,
    ) -> LMStudioResponse:
        url = f"{self._base_url}/v1/chat/completions"
        body = {
            "model": model_id,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_output_tokens,
            "messages": self._build_messages(
                system_prompt=system_prompt,
                developer_prompt=developer_prompt,
                user_context_json=user_context_json,
                images=images,
            ),
        }
        response = requests.post(url, json=body, timeout=self._timeout_sec)
        response.raise_for_status()
        raw = self._extract_text(response.json())
        return LMStudioResponse(
            raw_text=raw,
            latency_sec=response.elapsed.total_seconds(),
            mode="openai_compat",
        )

    def _build_messages(
        self,
        *,
        system_prompt: str,
        developer_prompt: str,
        user_context_json: str,
        images: list[tuple[str, bytes, str]],
    ) -> list[dict[str, Any]]:
        user_content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "Compact context JSON:\n" + user_context_json,
            }
        ]
        for label, image_bytes, mime_type in images:
            encoded = base64.b64encode(image_bytes).decode("ascii")
            user_content.append(
                {
                    "type": "text",
                    "text": f"Image label: {label}",
                }
            )
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{encoded}",
                    },
                }
            )

        return [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": developer_prompt},
            {"role": "user", "content": user_content},
        ]

    def _extract_text(self, payload: dict[str, Any]) -> str:
        if "choices" in payload and payload["choices"]:
            choice = payload["choices"][0]
            if isinstance(choice, dict):
                message = choice.get("message")
                if isinstance(message, dict):
                    content = message.get("content", "")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        texts = []
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                texts.append(str(item.get("text", "")))
                        return "\n".join(texts)
                if "text" in choice:
                    return str(choice["text"])
        if "message" in payload:
            message = payload["message"]
            if isinstance(message, dict):
                return str(message.get("content", ""))
            return str(message)
        if "content" in payload:
            return str(payload["content"])
        return str(payload)
