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
    def __init__(self, *, base_url: str, timeout_sec: float, api_key: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._api_key = api_key.strip()

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    def _openai_compat_url(self) -> str:
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/chat/completions"
        return f"{self._base_url}/v1/chat/completions"

    def _openai_compat_models_url(self) -> str:
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/models"
        return f"{self._base_url}/v1/models"

    def list_models(self) -> list[str]:
        response = requests.get(
            self._openai_compat_models_url(),
            headers=self._headers(),
            timeout=self._timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        rows: Any = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []

        model_ids: list[str] = []
        for row in rows:
            if isinstance(row, str):
                model_id = row.strip()
            elif isinstance(row, dict):
                model_id = str(row.get("id") or row.get("model") or row.get("name") or "").strip()
            else:
                model_id = ""
            if model_id and model_id not in model_ids:
                model_ids.append(model_id)
        return model_ids

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
        response_format: str = "",
        json_schema: dict[str, Any] | None = None,
        reasoning_effort: str = "",
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
                response_format=response_format,
                json_schema=json_schema,
                reasoning_effort=reasoning_effort,
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
                response_format=response_format,
                json_schema=json_schema,
                reasoning_effort=reasoning_effort,
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
        response = requests.post(
            url,
            json=body,
            headers=self._headers(),
            timeout=self._timeout_sec,
        )
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
        response_format: str,
        json_schema: dict[str, Any] | None,
        reasoning_effort: str,
    ) -> LMStudioResponse:
        url = self._openai_compat_url()
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
        if response_format == "json_schema" and json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "taskplanner_vlm_compact_v1",
                    "strict": True,
                    "schema": json_schema,
                },
            }
        elif response_format == "json_object":
            body["response_format"] = {"type": "json_object"}
        elif response_format == "text":
            body["response_format"] = {"type": "text"}
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        response = requests.post(
            url,
            json=body,
            headers=self._headers(),
            timeout=self._timeout_sec,
        )
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

        system_content = system_prompt
        if developer_prompt:
            system_content = f"{system_prompt}\n\n{developer_prompt}"

        return [
            {"role": "system", "content": system_content},
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
