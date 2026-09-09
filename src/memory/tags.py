"""Caller-supplied tags: one normalisation, used by every read and write.

Tag scoping is a convention (an agent is asked to pass `repo:<path>`), and a
convention breaks silently when the value stored and the value searched differ
by case or whitespace. Normalising in one place, applied identically on the
retain and the recall path, makes that class of bug unrepresentable rather
than merely unlikely.
"""

import re

from memory.errors import InvalidTag

# Derived server-side by retention._tags. A caller tag here would collide with
# typed curation and with the mental-model source filter, which selects on
# schema:ach-retain-v1 + validity:indefinite.
RESERVED_PREFIXES = frozenset({"type:", "basis:", "schema:", "validity:"})

MAX_TAGS = 8
MAX_TAG_LENGTH = 64

# One optional `namespace:` then a value. Slashes are allowed because the
# motivating tag is a forge path (`repo:group/sub/app`).
_TAG = re.compile(r"[a-z0-9][a-z0-9._-]*:?[a-z0-9][a-z0-9._/-]*")


def normalize_caller_tags(tags: list[str] | None) -> tuple[str, ...]:
    """Validate, lowercase, dedupe and sort. Raises InvalidTag on anything
    malformed, over-long, over-numerous, or in a server-owned namespace."""
    if not tags:
        return ()
    if len(tags) > MAX_TAGS:
        raise InvalidTag(f"at most {MAX_TAGS} tags")
    cleaned: set[str] = set()
    for raw in tags:
        tag = raw.strip().lower()
        if not tag or len(tag) > MAX_TAG_LENGTH or not _TAG.fullmatch(tag):
            raise InvalidTag("malformed tag")
        if any(tag.startswith(prefix) for prefix in RESERVED_PREFIXES):
            raise InvalidTag("that tag namespace is reserved")
        cleaned.add(tag)
    return tuple(sorted(cleaned))
