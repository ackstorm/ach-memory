"""Server-derived retain tags, and validation of caller-supplied ones (SPEC §3.2)."""

import re
from typing import Literal

from memory.errors import InvalidRequest

MemoryType = Literal["constraint", "preference", "decision", "convention", "fact", "gotcha"]
Basis = Literal["human_explicit", "agent_verified"]
SCHEMA_TAG = "schema:ach-retain-v1"
# Derived server-side; a caller tag here would collide with them.
RESERVED_PREFIXES = frozenset({"type:", "basis:", "schema:"})
MAX_TAGS = 8
MAX_TAG_LENGTH = 64
_TAG = re.compile(r"[a-z0-9][a-z0-9._-]*:?[a-z0-9][a-z0-9._/-]*")


def type_tag(memory_type: MemoryType) -> str:
    return f"type:{memory_type}"


def basis_tag(basis: Basis) -> str:
    return f"basis:{basis}"


def normalize_caller_tags(tags: object) -> tuple[str, ...]:
    """Validate, lowercase, dedupe and sort caller tags. Takes `object`, not
    `list[str]`: runs as a pydantic `mode="before"` validator, so the value
    arrives exactly as the caller sent it."""
    if tags is None:
        return ()
    if not isinstance(tags, list | tuple):
        raise InvalidRequest("tags must be a list of strings")
    if not tags:
        return ()
    if len(tags) > MAX_TAGS:
        raise InvalidRequest(f"at most {MAX_TAGS} tags")
    cleaned: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            raise InvalidRequest("tags must be a list of strings")
        tag = raw.strip().lower()
        if not tag or len(tag) > MAX_TAG_LENGTH or not _TAG.fullmatch(tag):
            raise InvalidRequest("malformed tag")
        if any(tag.startswith(prefix) for prefix in RESERVED_PREFIXES):
            raise InvalidRequest("that tag namespace is reserved")
        cleaned.add(tag)
    return tuple(sorted(cleaned))
