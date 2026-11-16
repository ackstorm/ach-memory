"""Calls to the model gateway (MEMORY_LLM_*): one JSON answer from an OpenAI-compatible chat
endpoint, and yes/no probabilities from its Decisions endpoint."""

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx

from memory.config import Settings


def configured(settings: Settings) -> bool:
    return bool(settings.llm_url)


# httpx bounds each phase of a request, not the whole; a worker with a deadline bounds the whole.
# ponytail: a call past its deadline keeps its worker until httpx gives up; more workers if slow
# calls ever queue behind each other.
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="deadline")


def within[T](seconds: float, call: Callable[..., T], *args, **kwargs) -> T:
    """`call(*args, **kwargs)`, or TimeoutError once `seconds` have passed."""
    return _pool.submit(call, *args, **kwargs).result(timeout=seconds)


def _headers(settings: Settings) -> dict[str, str]:
    header = settings.llm_header.strip() or "Authorization"
    key = settings.llm_api_key
    return {"Authorization": f"Bearer {key}"} if header.lower() == "authorization" else {header: key}


def decide(settings: Settings, text: str, questions: list[str]) -> list[float]:
    """The probability that each yes/no question holds of `text`, in order: one request to the
    gateway's Decisions endpoint (`MEMORY_LLM_DECISION_MODEL`). Raises on any failure."""
    response = httpx.post(
        f"{settings.llm_url.rstrip('/')}/decisions",
        headers=_headers(settings),
        timeout=settings.llm_timeout_seconds,
        json={"model": settings.llm_decision_model, "input": text,
              "questions": [{"type": "predicate", "name": f"q{i}", "instructions": q}
                            for i, q in enumerate(questions)]},
    )
    response.raise_for_status()
    answers = {a["name"]: float(a["probability"]) for a in response.json()["answers"]}
    return [answers[f"q{i}"] for i in range(len(questions))]


def complete_json(settings: Settings, prompt: str) -> dict:
    """Low reasoning effort, JSON mode. Raises on transport errors or an unreadable answer."""
    response = httpx.post(
        f"{settings.llm_url.rstrip('/')}/chat/completions",
        headers=_headers(settings),
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
