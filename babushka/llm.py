import json
from typing import AsyncIterator

import httpx

from . import config


class LLMError(Exception):
    pass


async def _stream_model(client: httpx.AsyncClient, model: str, messages: list[dict]) -> AsyncIterator[str]:
    payload = {"model": model, "messages": messages, "stream": True, "temperature": 0.9}
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    async with client.stream("POST", f"{config.BASE_URL}/chat/completions", json=payload, headers=headers) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode("utf-8", "replace")[:300]
            raise LLMError(f"{model}: HTTP {resp.status_code} {body}")
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise LLMError(f"{model}: {chunk['error']}")
            for choice in chunk.get("choices") or []:
                # reasoning-модели шлют мысли отдельным полем - его не показываем
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    yield piece


async def stream_reply(messages: list[dict]) -> AsyncIterator[str]:
    """Стримит ответ; при ошибке до первого токена пробует следующую модель."""
    errors: list[str] = []
    timeout = httpx.Timeout(connect=15, read=60, write=15, pool=15)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for model in config.MODELS:
            started = False
            try:
                async for piece in _stream_model(client, model, messages):
                    started = True
                    yield piece
                if started:
                    return
                errors.append(f"{model}: пустой ответ")
            except (LLMError, httpx.HTTPError) as e:
                if started:
                    raise
                errors.append(str(e) or type(e).__name__)
    raise LLMError("; ".join(errors))
