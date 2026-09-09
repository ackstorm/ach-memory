"""Caller-supplied tags: one normalisation, used by every read and write.

Tag scoping is a convention (an agent is asked to pass `repo:<path>`), and a
convention breaks silently when the value stored and the value searched differ
by case or whitespace. Normalising in one place, applied identically on the
retain and the recall path, makes that class of bug unrepresentable rather
than merely unlikely.
"""

import re
from typing import Literal

from memory.errors import InvalidTag

# Derived server-side by retention._tags. A caller tag here would collide with
# typed curation and with the mental-model source filter, which selects on
# schema:ach-retain-v1 + validity:indefinite.
RESERVED_PREFIXES = frozenset({"type:", "basis:", "schema:", "validity:"})

MAX_TAGS = 8
MAX_TAG_LENGTH = 64

# Caller-facing tag filter modes. Both are strict: a mode that admits
# untagged memories is not a filter, so the non-strict Hindsight vocabulary
# (`all`/`any` without `_strict`) is never exposed here -- see `to_upstream`.
FilterMode = Literal["all", "any"]
FILTER_MODES: tuple[FilterMode, ...] = ("all", "any")

_UPSTREAM_MODE: dict[FilterMode, str] = {"all": "all_strict", "any": "any_strict"}


def default_filter_mode() -> FilterMode:
    """The safe default: a caller who never thinks about the mode narrows,
    rather than silently getting back the whole corpus."""
    return "all"


def to_upstream(mode: FilterMode) -> str:
    """Map a caller-facing mode to Hindsight's own `tags_match` vocabulary.
    Always the `_strict` variant: the non-strict forms also return untagged
    memories, which would defeat a filter without saying so."""
    return _UPSTREAM_MODE[mode]

# One optional `namespace:` then a value. Slashes are allowed because the
# motivating tag is a forge path (`repo:group/sub/app`).
_TAG = re.compile(r"[a-z0-9][a-z0-9._-]*:?[a-z0-9][a-z0-9._/-]*")


def normalize_caller_tags(tags: object) -> tuple[str, ...]:
    """Validate, lowercase, dedupe and sort. Raises InvalidTag on anything
    malformed, over-long, over-numerous, or in a server-owned namespace.

    Takes `object`, not `list[str]`, on purpose: the pydantic models that use
    this run it as a `mode="before"` validator, so the value arrives exactly
    as the caller sent it and has NOT been type-checked yet. A `TypeError` or
    `AttributeError` raised here is not a `DomainError`, so it would escape
    pydantic and land in the app's catch-all as a 500 -- a malformed request
    body reported as a server fault. Every rejection below is an `InvalidTag`.
    """
    if tags is None:
        return ()
    if not isinstance(tags, list | tuple):
        raise InvalidTag("tags must be a list of strings")
    if not tags:
        return ()
    if len(tags) > MAX_TAGS:
        raise InvalidTag(f"at most {MAX_TAGS} tags")
    cleaned: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            raise InvalidTag("tags must be a list of strings")
        tag = raw.strip().lower()
        if not tag or len(tag) > MAX_TAG_LENGTH or not _TAG.fullmatch(tag):
            raise InvalidTag("malformed tag")
        if any(tag.startswith(prefix) for prefix in RESERVED_PREFIXES):
            raise InvalidTag("that tag namespace is reserved")
        cleaned.add(tag)
    return tuple(sorted(cleaned))
