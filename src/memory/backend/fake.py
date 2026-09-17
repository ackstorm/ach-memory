"""In-memory backend adapter: the test double for the core test suite (SPEC §4).

Emulates no memory intelligence -- recall is plain token overlap, nothing more.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from memory.backend.base import (
    Backend,
    Capability,
    Hit,
    MemoryState,
    MemoryView,
    MentalModelView,
    Page,
    TagGroup,
    WriteAck,
    matches_tag_groups,
)
from memory.builtin_models import BuiltinModel
from memory.errors import MemoryNotFound

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


@dataclass
class _Record:
    text: str
    tags: tuple[str, ...]
    metadata: dict
    state: MemoryState
    created_at: str


class FakeBackend(Backend):
    """In-memory `Backend` implementation. No threading, no persistence."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, _Record]] = {}
        self.mental_models: dict[str, dict[str, BuiltinModel]] = {}
        #: content a test wants `get_mental_model` to return; unset means None.
        self.mental_model_content: dict[str, dict[str, str | None]] = {}

    def reset(self) -> None:
        self._data = {}
        self.mental_models = {}
        self.mental_model_content = {}

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({"mental_models"})

    def provision(self, bank_id: str) -> None:
        self._data.setdefault(bank_id, {})

    def retain(self, bank_id: str, memory_id: str, content: str, *, tags: tuple[str, ...],
               metadata: dict, wait: bool) -> WriteAck:
        """`wait=False` still stores the memory immediately -- the fake does not simulate async
        completion beyond the returned status, which is `"pending"` for `wait=False` and
        `"completed"` for `wait=True`."""
        records = self._data.setdefault(bank_id, {})
        now = datetime.now(UTC).isoformat()
        record = records.get(memory_id)
        if record is None:
            records[memory_id] = _Record(text=content, tags=tags, metadata=dict(metadata),
                                          state="valid", created_at=now)
        else:
            record.text = content
            record.tags = tags
            record.metadata = dict(metadata)
            record.state = "valid"
        return WriteAck(memory_id=memory_id, status="completed" if wait else "pending")

    def recall(self, bank_id: str, query: str, *, tag_groups: tuple[TagGroup, ...],
               limit: int) -> list[Hit]:
        records = self._data.get(bank_id, {})
        q = _tokens(query)
        scored: list[tuple[float, str]] = []
        for memory_id, record in records.items():
            if record.state != "valid":
                continue
            if not matches_tag_groups(record.tags, tag_groups):
                continue
            t = _tokens(record.text)
            union = q | t
            score = len(q & t) / len(union) if union else 0.0
            if score == 0:
                continue
            scored.append((score, memory_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        hits = []
        for score, memory_id in scored[:limit]:
            record = records[memory_id]
            hits.append(Hit(memory_id=memory_id, text=record.text, score=score,
                             tags=record.tags, metadata=dict(record.metadata)))
        return hits

    def invalidate(self, bank_id: str, memory_id: str, *, reason: str | None) -> None:
        record = self._data.get(bank_id, {}).get(memory_id)
        if record is None:
            raise MemoryNotFound()
        record.state = "invalidated"

    def revalidate(self, bank_id: str, memory_id: str) -> None:
        record = self._data.get(bank_id, {}).get(memory_id)
        if record is None:
            raise MemoryNotFound()
        record.state = "valid"

    def delete(self, bank_id: str, memory_id: str) -> None:
        self._data.get(bank_id, {}).pop(memory_id, None)

    def list(self, bank_id: str, *, tag_groups: tuple[TagGroup, ...], state: MemoryState | None,
             limit: int, offset: int) -> Page:
        wanted_state = state or "valid"
        records = self._data.get(bank_id, {})
        matching = [
            (memory_id, record)
            for memory_id, record in records.items()
            if record.state == wanted_state and matches_tag_groups(record.tags, tag_groups)
        ]
        # Newest first (base.py's `list` contract): `records` preserves insertion order and a
        # replace via `retain` keeps a memory's original position, so reversing it is enough.
        matching.reverse()
        page = matching[offset:offset + limit]
        items = tuple(
            MemoryView(memory_id=memory_id, text=record.text, state=record.state,
                       tags=record.tags, metadata=dict(record.metadata),
                       created_at=record.created_at)
            for memory_id, record in page
        )
        return Page(items=items, total=len(matching))

    def get(self, bank_id: str, memory_id: str) -> MemoryView | None:
        record = self._data.get(bank_id, {}).get(memory_id)
        if record is None:
            return None
        return MemoryView(memory_id=memory_id, text=record.text, state=record.state,
                           tags=record.tags, metadata=dict(record.metadata),
                           created_at=record.created_at)

    def provision_mental_models(self, bank_id: str, builtins: Sequence[BuiltinModel]) -> None:
        models = self.mental_models.setdefault(bank_id, {})
        for builtin in builtins:
            models[builtin.key] = builtin

    def get_mental_model(self, bank_id: str, key: str) -> MentalModelView | None:
        builtin = self.mental_models.get(bank_id, {}).get(key)
        if builtin is None:
            return None
        content = self.mental_model_content.get(bank_id, {}).get(key)
        return MentalModelView(key=key, name=builtin.name, content=content)


#: `get_backend()`'s "fake" registration: one process-wide instance, reset between tests
#: by `tests/conftest.py`'s `_isolated_fake_backend` fixture.
fake_backend = FakeBackend()
