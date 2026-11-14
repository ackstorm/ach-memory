"""One JSON answer from an OpenAI-compatible chat endpoint (MEMORY_LLM_*)."""

import json
import re

import httpx

from memory.config import Settings


def configured(settings: Settings) -> bool:
    return bool(settings.llm_url)


def complete_json(settings: Settings, prompt: str) -> dict:
    """Low reasoning effort, JSON mode. Raises on transport errors or an unreadable answer."""
    header = settings.llm_header.strip() or "Authorization"
    key = settings.llm_api_key
    headers = {"Authorization": f"Bearer {key}"} if header.lower() == "authorization" else {header: key}
    response = httpx.post(
        f"{settings.llm_url.rstrip('/')}/chat/completions",
        headers=headers,
        timeout=settings.llm_timeout_seconds,
        json={
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "reasoning_effort": "low",
            "response_format": {"type": "json_object"},
        },
    )
    response.raise_for_status()
    text = response.json()["choices"][0]["message"]["content"] or ""
    # Some models wrap the object in prose or a code fence.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0) if match else text)
