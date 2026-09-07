"""Mechanical, non-semantic sanitization for typed retain claims and evidence.

Canonical content is REJECTED, never rewritten, when it would need secret
redaction: a claim is one durable, independently-correctable statement, and
silently editing it would make the stored text diverge from what the caller
believes it retained. Evidence is redacted in place instead -- it is bounded,
disposable context, not the record of truth -- but an item that redacts to
nothing is dropped, and a request with no meaningful survivor is rejected.
"""

from __future__ import annotations

import json
import re
import unicodedata

from pydantic import BaseModel, ConfigDict

from memory.errors import ContentRejectedBySanitizer, ContentTooLarge
from memory.identifiers import has_control_character
from memory.memory_types import EvidenceKind
from memory.v040_contracts import RetainEvidence

_HORIZONTAL_WS = re.compile(r"[ \t]+")

_MAX_BYTES = 4096

# Structural, not semantic: fixed patterns for shapes secrets commonly take.
# Shared sanitization redacts the same shapes out of
# transcript text before it ever crosses the network.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{10,}"),
    re.compile(r"\b(?:sk|gh[oprsu]|mem|xox[baprs])[-_][A-Za-z0-9]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@\S+"),
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+@\S+"),
    re.compile(r"(?i)\b\w*(?:secret|password|passwd|token|api[_-]?key)\w*\s*[=:]\s*\S+"),
]


# Scanning cost is QUADRATIC in input length: the key=value rule above ends
# in `\w*` either side of the literal, so on a long run of word characters
# the engine retries from every position. Measured on this codebase:
# 4 KB 20 ms, 8 KB 79 ms, 16 KB 324 ms, 32 KB 1.3 s -- roughly 4x per
# doubling, which puts 1 MB near twenty minutes of single-threaded CPU.
#
# `normalize_claim` used to normalize and scan first and check the ceiling
# afterwards, so one authenticated retain could pin a worker for as long as
# it liked; `TypedRetainRequest.content` carries no max_length, and the
# write budget is 60 requests per minute per credential. The size gate has
# to come BEFORE any scan.
#
# Four times _MAX_BYTES rather than _MAX_BYTES: normalization legitimately
# shrinks input, since runs of horizontal whitespace collapse to a single
# space, so a raw 16 KB body may still land under the 4096-byte limit and
# must keep being accepted. The post-normalization check stays the real
# limit; this one only bounds what the scanner is ever handed.
_MAX_RAW_BYTES = _MAX_BYTES * 4


def _reject_unscannable(text: str) -> None:
    """Refuse input too large to scan affordably, before scanning it."""
    if len(text.encode("utf-8")) > _MAX_RAW_BYTES:
        raise ContentTooLarge(f"content exceeds {_MAX_RAW_BYTES} bytes before normalization")


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def sanitize_ref(value: str | None) -> str | None:
    """A best-effort evidence pointer, or None if it cannot be trusted as one."""
    if value is None:
        return None
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or has_control_character(normalized):
        return None
    return normalized


class SanitizedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: EvidenceKind
    raw: str
    source_ref: str | None = None


def normalize_claim(content: str) -> str:
    """Canonicalize one durable claim. Rejects rather than rewrites a secret."""
    _reject_unscannable(content)
    normalized = unicodedata.normalize("NFC", content).replace("\r\n", "\n")
    normalized = "\n".join(
        _HORIZONTAL_WS.sub(" ", line).rstrip() for line in normalized.split("\n")
    )
    if not normalized.strip() or contains_secret(normalized):
        raise ContentRejectedBySanitizer("canonical content cannot be stored safely")
    if len(normalized.encode("utf-8")) > _MAX_BYTES:
        raise ContentTooLarge(f"content exceeds {_MAX_BYTES} bytes")
    return normalized


def sanitize_evidence(items: tuple[RetainEvidence, ...]) -> tuple[SanitizedEvidence, ...]:
    """Redact each evidence item independently; drop any that redact to
    nothing; require at least one meaningful survivor."""
    kept: list[SanitizedEvidence] = []
    for item in items:
        # RetainEvidence.raw is capped at 1024 characters by the schema, so
        # this is defence in depth rather than a live exposure -- but the
        # bound lives in another module and this is the function that pays
        # the quadratic cost if it ever moves.
        _reject_unscannable(item.raw)
        raw = redact_secrets(unicodedata.normalize("NFC", item.raw).replace("\r\n", "\n"))
        if raw.strip() and raw.strip() != "[redacted]":
            kept.append(
                SanitizedEvidence(kind=item.kind, raw=raw, source_ref=sanitize_ref(item.source_ref))
            )
    if not kept:
        raise ContentRejectedBySanitizer("at least one meaningful evidence item is required")
    encoded = json.dumps([item.model_dump() for item in kept]).encode("utf-8")
    if len(encoded) > _MAX_BYTES:
        raise ContentTooLarge("evidence exceeds 4096 bytes")
    return tuple(kept)
