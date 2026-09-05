"""Deterministic, whole-entry bounded context delivery."""

from collections.abc import Iterable
from dataclasses import dataclass

import tiktoken
from pydantic import BaseModel, ConfigDict

TOKENIZER_VERSION = "ach-delivery-o200k-v1"
_ENCODING = tiktoken.get_encoding("o200k_base")


def count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text, disallowed_special=()))


@dataclass(frozen=True)
class DeliverySection:
    key: str
    heading: str
    text: str
    max_tokens: int


class DeliveryOmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    reason: str
    token_count: int | None = None
    omitted_count: int | None = None


class ContextPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    tokenizer_version: str = TOKENIZER_VERSION
    total_tokens: int
    headings: list[str] = []
    omissions: list[DeliveryOmission] = []


def _inert(value: str) -> str:
    # Content is data; newline-delimited section framing must remain owned by
    # the assembler even when upstream output contains forged headings.
    return value.replace("\r", " ").replace("\n", " ").strip()


def assemble_context(
    sections: Iterable[DeliverySection], *, global_max_tokens: int = 4608
) -> ContextPayload:
    ordered = sorted(sections, key=lambda section: section.key)
    rendered: list[tuple[DeliverySection, str]] = []
    headings: list[str] = []
    omissions: list[DeliveryOmission] = []
    for section in ordered:
        body = _inert(section.text)
        candidate = f"{_inert(section.heading)}\n{body}"
        body_tokens = count_tokens(body)
        if body_tokens > section.max_tokens:
            omissions.append(DeliveryOmission(key=section.key, reason="model_output_over_budget", token_count=body_tokens))
            continue
        rendered.append((section, candidate))
        headings.append(_inert(section.heading))
    text = "\n\n".join(candidate for _, candidate in rendered)
    total = count_tokens(text)
    if total > global_max_tokens:
        # Omit whole entries from the end, preserving deterministic priority.
        while rendered and count_tokens("\n\n".join(candidate for _, candidate in rendered)) > global_max_tokens:
            section, _ = rendered.pop()
            headings.pop()
            omissions.append(DeliveryOmission(key=section.key, reason="global_budget"))
        text = "\n\n".join(candidate for _, candidate in rendered)
        total = count_tokens(text)
    return ContextPayload(text=text, total_tokens=total, headings=headings, omissions=omissions)
