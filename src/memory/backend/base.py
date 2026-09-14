"""Backend adapter contract (SPEC §4). ACH nouns only; engine nouns stay inside adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from memory.builtin_models import BuiltinModel
from memory.errors import UnsupportedCapability

Capability = Literal["synthesis", "operations", "mental_models"]
MemoryState = Literal["valid", "invalidated"]
WriteStatus = Literal["completed", "pending", "unknown"]

#: {"tags": [...], "match": "any" | "all"}; groups are ANDed. Adapters translate.
TagGroup = dict[str, object]


def matches_tag_groups(tags: tuple[str, ...], tag_groups: tuple[TagGroup, ...]) -> bool:
    """AND of `tag_groups`. ACH builds groups itself, so a malformed one (empty `tags`, or
    `match` other than "any"/"all") is a programming error, not caller input."""
    tag_set = set(tags)
    for group in tag_groups:
        group_tags = set(group.get("tags") or [])
        match = group.get("match")
        if not group_tags:
            raise ValueError(f"tag group has no tags: {group!r}")
        if match == "all":
            if not group_tags <= tag_set:
                return False
        elif match == "any":
            if not group_tags & tag_set:
                return False
        else:
            raise ValueError(f"tag group has an invalid match {match!r}: {group!r}")
    return True


@dataclass(frozen=True)
class Hit:
    memory_id: str
    text: str
    score: float | None          # adapter rank score; the semantic floor is per-adapter config
    tags: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)
    mentioned_at: str | None = None


@dataclass(frozen=True)
class MemoryView:
    memory_id: str
    text: str
    state: MemoryState
    tags: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)
    created_at: str | None = None


@dataclass(frozen=True)
class WriteAck:
    memory_id: str
    status: WriteStatus
    operation_ref: str | None = None   # opaque engine reference, for the `operations` capability


@dataclass(frozen=True)
class Page:
    items: tuple[MemoryView, ...]
    total: int


@dataclass(frozen=True)
class MentalModelView:
    key: str
    name: str
    content: str | None
    updated_at: str | None = None


class Backend(ABC):
    @abstractmethod
    def capabilities(self) -> frozenset[Capability]:
        """Which optional intentions this adapter serves (SPEC §3.1); callers check before
        using them."""

    @abstractmethod
    def provision(self, bank_id: str) -> None:
        """Ensure the bank exists and carries the adapter's deployment configuration; other
        methods may assume it. Adapters may also create lazily (the fake does; Hindsight
        auto-creates a bank on first write)."""

    @abstractmethod
    def retain(self, bank_id: str, memory_id: str, content: str, *, tags: tuple[str, ...],
               metadata: dict, wait: bool) -> WriteAck:
        """Write one memory under `memory_id`; retaining an existing id replaces its content, tags
        and metadata and revalidates it (an invalidated memory becomes valid again)."""

    @abstractmethod
    def recall(self, bank_id: str, query: str, *, tag_groups: tuple[TagGroup, ...],
               limit: int) -> list[Hit]:
        """Ranked hits for `query` in `bank_id`, filtered by `tag_groups` (ANDed), capped at
        `limit`. Never returns invalidated memories."""

    @abstractmethod
    def invalidate(self, bank_id: str, memory_id: str, *, reason: str | None) -> None:
        """Soft-invalidate one memory; unknown id raises `MemoryNotFound`, already-invalidated is
        a no-op."""

    @abstractmethod
    def revalidate(self, bank_id: str, memory_id: str) -> None:
        """Reverse `invalidate`; unknown id raises `MemoryNotFound`, already-valid is a no-op."""

    @abstractmethod
    def delete(self, bank_id: str, memory_id: str) -> None:
        """Hard-erase one memory; idempotent, an unknown id is not an error."""

    @abstractmethod
    def list(self, bank_id: str, *, tag_groups: tuple[TagGroup, ...], state: MemoryState | None,
             limit: int, offset: int) -> Page:
        """Page memories in `bank_id` filtered by `tag_groups`; `state=None` means valid only,
        `state="invalidated"` means invalidated only. `total` counts all matches before paging.
        Newest first."""

    @abstractmethod
    def get(self, bank_id: str, memory_id: str) -> MemoryView | None:
        """One memory by id, or `None` if it does not exist (valid or invalidated either way)."""

    # optional capabilities — raise UnsupportedCapability when absent
    def reflect(self, bank_id: str, query: str, *, tag_groups: tuple[TagGroup, ...]) -> str:
        """One synthesized answer over `bank_id`, scoped by `tag_groups` (capability
        `synthesis`)."""
        raise UnsupportedCapability("synthesis")

    def get_operation(self, bank_id: str, operation_ref: str) -> dict:
        """Engine-shaped dict with bank ids redacted; ACH passes it through unchanged
        (capability `operations`)."""
        raise UnsupportedCapability("operations")

    def list_operations(self, bank_id: str, *, status: str | None, limit: int, offset: int) -> dict:
        """Engine-shaped dict with bank ids redacted; ACH passes it through unchanged
        (capability `operations`)."""
        raise UnsupportedCapability("operations")

    def cancel_operation(self, bank_id: str, operation_ref: str) -> dict:
        """Engine-shaped dict with bank ids redacted; ACH passes it through unchanged
        (capability `operations`)."""
        raise UnsupportedCapability("operations")

    def provision_mental_models(self, bank_id: str, builtins: Sequence[BuiltinModel]) -> None:
        """Ensure each of `builtins` exists in `bank_id`, created or updated to its current
        prompt; idempotent (capability `mental_models`)."""
        raise UnsupportedCapability("mental_models")

    def get_mental_model(self, bank_id: str, key: str) -> MentalModelView | None:
        """Current content of one mental model, or `None` if absent (capability `mental_models`)."""
        raise UnsupportedCapability("mental_models")
