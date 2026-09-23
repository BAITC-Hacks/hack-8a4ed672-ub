"""Client for the LOCAL llama.cpp server. OpenAI-compatible wire format does NOT mean a cloud call:
the base URL is validated against the administrator's allowlist of internal hosts at construction time.
The model gets no tools, no shell, no network; it only returns JSON constrained by a schema."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from hattama.security.urls import validate_llm_base_url

log = logging.getLogger(__name__)


class LlmUnavailableError(RuntimeError):
    """Local LLM endpoint is not reachable/ready (never triggers a cloud fallback)."""


class LlmOutputError(RuntimeError):
    """Model answered, but not with valid JSON for the schema."""


@dataclass
class LlmCall:
    content: str
    prompt_tokens: int | None
    completion_tokens: int | None
    seconds: float
    finish_reason: str | None


@dataclass(frozen=True)
class SamplingParams:
    temperature: float = 0.2
    top_p: float = 0.8
    top_k: int = 20
    min_p: float = 0.0
    seed: int = 42
    max_tokens: int = 1200


class LlamaCppClient:
    def __init__(self, base_url: str, allowed_hosts: list[str], timeout_s: float = 300.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = validate_llm_base_url(base_url, allowed_hosts)
        self._client = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(timeout_s, connect=5.0),
                                    transport=transport, trust_env=False, follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def health(self) -> dict[str, Any]:
        try:
            r = self._client.get("/health")
        except httpx.HTTPError as exc:
            raise LlmUnavailableError(f"LLM-сервер недоступен ({self.base_url}): {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise LlmUnavailableError(f"LLM-сервер не готов: HTTP {r.status_code}")
        return r.json()

    def model_name(self) -> str | None:
        try:
            r = self._client.get("/v1/models")
            data = r.json().get("data") or []
            return data[0].get("id") if data else None
        except (httpx.HTTPError, ValueError):
            return None

    def count_tokens(self, text: str) -> int:
        try:
            r = self._client.post("/tokenize", json={"content": text})
            r.raise_for_status()
            return len(r.json()["tokens"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise LlmUnavailableError(f"tokenize недоступен: {type(exc).__name__}") from exc

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any],
                  params: SamplingParams = SamplingParams()) -> LlmCall:
        body = {
            "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": schema,
                                                                       "strict": True}},
            "temperature": params.temperature,
            "top_p": params.top_p,
            "top_k": params.top_k,
            "min_p": params.min_p,
            "seed": params.seed,
            "max_tokens": params.max_tokens,
            "stream": False,
            # Qwen3: disable the thinking block; constrained JSON output only.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        started = time.perf_counter()
        try:
            r = self._client.post("/v1/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LlmUnavailableError(f"LLM-сервер недоступен: {type(exc).__name__}") from exc
        if r.status_code == 503:
            raise LlmUnavailableError("LLM-сервер загружает модель (HTTP 503)")
        if r.status_code >= 400:
            raise LlmUnavailableError(f"LLM-сервер вернул HTTP {r.status_code}")
        data = r.json()
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        content = choice["message"].get("content") or ""
        return LlmCall(content=content, prompt_tokens=usage.get("prompt_tokens"),
                       completion_tokens=usage.get("completion_tokens"), seconds=time.perf_counter() - started,
                       finish_reason=choice.get("finish_reason"))


def parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmOutputError(f"невалидный JSON: {exc.msg} (позиция {exc.pos})") from exc
    if not isinstance(data, dict):
        raise LlmOutputError("ожидался JSON-объект")
    return data
