"""Hindsight backend adapter (SPEC §4-§5): the production `Backend`, HTTP client folded in.

Translates ACH nouns (`memory_id`, `tag_groups` with `match` "any"/"all") to Hindsight's own
(`document_id`, `tag_groups` with `match` "any_strict"/"all_strict"). Engine nouns stay in here.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

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
from memory.builtin_models import BUILTIN_MODELS, BuiltinModel
from memory.config import get_settings
from memory.errors import InvalidRequest, MemoryNotFound, NotFound, UpstreamError

logger = logging.getLogger("memory.backend.hindsight")

# hindsight-api registers every bank route under the literal segment `default`; multi-tenancy
# upstream is resolved from the Authorization header, never the URL.
_TENANT = "default"


def _bank(bank_id: str) -> str:
    return f"/v1/{_TENANT}/banks/{bank_id}"


def _present(values: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the caller left unset -- `state=None` as a query param renders as an empty
    string, which Hindsight filters on, unlike an omitted key."""
    return {k: v for k, v in values.items() if v is not None}


def _derive_failed(record: dict) -> dict:
    """Report `failed` for an operation whose every child already errored.

    Hindsight leaves the parent `pending`/`running` forever when every child_operation carries
    an error_message (measured live, hindsight-api 0.9.1) -- a caller polling for a terminal
    status would wait forever otherwise.
    """
    children = record.get("child_operations") or []
    if record.get("status") not in ("pending", "running") or not children:
        return record
    if all(child.get("error_message") for child in children):
        return {**record, "status": "failed"}
    return record


def _translate_tag_group(group: TagGroup) -> dict[str, Any]:
    """ACH `"any"`/`"all"` -> Hindsight recall/reflect's `"any_strict"`/`"all_strict"`."""
    ach_match = group.get("match")
    if ach_match == "any":
        match = "any_strict"
    elif ach_match == "all":
        match = "all_strict"
    else:
        raise ValueError(f"tag group has an invalid match {ach_match!r}: {group!r}")
    return {"tags": list(group.get("tags") or []), "match": match}


def _public_metadata(metadata: dict) -> dict:
    """Strip the adapter's own `memory_id` bookkeeping key before it reaches a caller."""
    return {k: v for k, v in metadata.items() if k != "memory_id"}


def _strip_bank_id(value: Any, bank_id: str) -> Any:
    """Hindsight echoes bank_id, sometimes embedded inside another field (e.g. `chunk_id`); it
    must never reach a caller (SPEC I8). Recursive: recall/list return nested items."""
    if isinstance(value, dict):
        return {k: _strip_bank_id(v, bank_id) for k, v in value.items() if k != "bank_id"}
    if isinstance(value, list):
        return [_strip_bank_id(item, bank_id) for item in value]
    if isinstance(value, str) and bank_id in value:
        return value.replace(bank_id, "REDACTED")
    return value


class HindsightBackend(Backend):
    """Production `Backend` implementation, talking straight to the Hindsight HTTP API."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        """`transport` is a test seam (`httpx.MockTransport`); production always uses `None`,
        the real network."""
        settings = get_settings()
        self._extraction_mode = settings.hindsight_extraction_mode
        headers = {"Authorization": f"Bearer {settings.hindsight_api_key}"} \
            if settings.hindsight_api_key else {}
        # Connect stays short whatever the call is -- an unreachable backend must fail fast.
        # Only the read side is extended, and only for calls that wait on a model.
        self._default_timeout = httpx.Timeout(settings.hindsight_timeout_seconds, connect=5.0)
        self._llm_timeout = httpx.Timeout(settings.hindsight_llm_timeout_seconds, connect=5.0)
        self._http = httpx.Client(
            base_url=settings.hindsight_url, headers=headers, timeout=self._default_timeout,
            transport=transport,
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, *,
                 params: dict[str, Any] | None = None, not_found: type[Exception] | None = None,
                 timeout: httpx.Timeout | None = None) -> dict:
        try:
            response = self._http.request(method, path, json=payload, params=params,
                                            timeout=timeout or self._default_timeout)
        except httpx.HTTPError as exc:
            # Never attach the httpx exception: it holds .request.url, which names the bank.
            logger.warning("hindsight transport failure: %s", type(exc).__name__)
            raise UpstreamError("memory backend unreachable") from None

        if response.status_code == 404 and not_found is not None:
            raise not_found("no such object in this memory")
        if response.status_code >= 500:
            logger.warning("hindsight server error: %s", response.status_code)
            raise UpstreamError("memory backend rejected the request")
        if response.status_code >= 400:
            logger.warning("hindsight rejected the request: %s", response.status_code)
            raise InvalidRequest("memory backend rejected this request shape")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise UpstreamError("memory backend returned an unreadable response") from None

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({"synthesis", "operations", "mental_models"})

    def provision(self, bank_id: str) -> None:
        self._request("PUT", _bank(bank_id), {})
        cfg = (self._request("GET", f"{_bank(bank_id)}/config") or {}).get("config") or {}
        if cfg.get("retain_extraction_mode") == self._extraction_mode:
            return
        self._request("PATCH", f"{_bank(bank_id)}/config",
                       {"updates": {"retain_extraction_mode": self._extraction_mode}})
        cfg = (self._request("GET", f"{_bank(bank_id)}/config") or {}).get("config") or {}
        if cfg.get("retain_extraction_mode") != self._extraction_mode:
            raise UpstreamError("memory backend extraction mode could not be verified")

    def retain(self, bank_id: str, memory_id: str, content: str, *, tags: tuple[str, ...],
               metadata: dict, wait: bool) -> WriteAck:
        item = {
            "content": content,
            "document_id": memory_id,
            "metadata": {**metadata, "memory_id": memory_id},
            "tags": list(tags),
            "update_mode": "replace",
        }
        body = {"items": [item], "async": not wait, "operation_id": str(uuid.uuid4())}
        response = self._request(
            "POST", f"{_bank(bank_id)}/memories", body,
            timeout=self._llm_timeout if wait else None,
        )
        return WriteAck(memory_id=memory_id, status="completed" if wait else "pending",
                         operation_ref=(response or {}).get("operation_id"))

    def recall(self, bank_id: str, query: str, *, tag_groups: tuple[TagGroup, ...],
               limit: int) -> list[Hit]:
        if limit <= 0:
            return []
        translated = [_translate_tag_group(g) for g in tag_groups]
        body: dict[str, Any] = {
            "query": query, "types": ["world"], "include": {"entities": None},
            "max_tokens": 32768,
        }
        if translated:
            body["tag_groups"] = translated
        response = self._request("POST", f"{_bank(bank_id)}/memories/recall", body)
        hits: list[Hit] = []
        seen: set[str] = set()
        for raw in (response or {}).get("results") or []:
            metadata = dict(raw.get("metadata") or {})
            memory_id = metadata.get("memory_id")
            if memory_id is None or memory_id in seen:
                continue
            seen.add(memory_id)
            scores = raw.get("scores") or {}
            hits.append(Hit(
                memory_id=memory_id, text=raw.get("text", ""), score=scores.get("semantic"),
                tags=tuple(raw.get("tags") or ()), metadata=_public_metadata(metadata),
                mentioned_at=raw.get("mentioned_at"),
            ))
            if len(hits) >= limit:
                break
        return hits

    def _units(self, bank_id: str, memory_id: str, state: str | None = None) -> list[dict]:
        # type="world" excludes a document's derived observation twin: returning or curating it
        # would leak synthesized text through get()/list() or 400 out of curate().
        filters = _present({"document_id": memory_id, "type": "world", "limit": 100,
                             "state": state})
        response = self._request("GET", f"{_bank(bank_id)}/memories/list", params=filters)
        return (response or {}).get("items") or []

    def invalidate(self, bank_id: str, memory_id: str, *, reason: str | None) -> None:
        valid = self._units(bank_id, memory_id)
        if not valid:
            if self._units(bank_id, memory_id, state="invalidated"):
                return
            raise MemoryNotFound(memory_id=memory_id)
        # One PATCH per unit: a failure partway through leaves the memory half-switched, but
        # the outcome lands in the ACH journal either way, and retrying is safe.
        for unit in valid:
            self._request("PATCH", f"{_bank(bank_id)}/memories/{unit['id']}",
                           _present({"state": "invalidated", "reason": reason}),
                           not_found=MemoryNotFound)

    def revalidate(self, bank_id: str, memory_id: str) -> None:
        invalidated = self._units(bank_id, memory_id, state="invalidated")
        if not invalidated:
            if self._units(bank_id, memory_id):
                return
            raise MemoryNotFound(memory_id=memory_id)
        for unit in invalidated:
            self._request("PATCH", f"{_bank(bank_id)}/memories/{unit['id']}",
                           {"state": "valid"}, not_found=MemoryNotFound)

    def delete(self, bank_id: str, memory_id: str) -> None:
        try:
            self._request("DELETE", f"{_bank(bank_id)}/documents/{memory_id}",
                           not_found=NotFound)
        except NotFound:
            pass

    def get(self, bank_id: str, memory_id: str) -> MemoryView | None:
        valid = self._units(bank_id, memory_id)
        if valid:
            return self._to_view(memory_id, valid[0], "valid")
        invalidated = self._units(bank_id, memory_id, state="invalidated")
        if invalidated:
            return self._to_view(memory_id, invalidated[0], "invalidated")
        return None

    @staticmethod
    def _to_view(memory_id: str, unit: dict, state: MemoryState) -> MemoryView:
        return MemoryView(
            memory_id=memory_id, text=unit.get("text", ""), state=state,
            tags=tuple(unit.get("tags") or ()),
            metadata=_public_metadata(unit.get("metadata") or {}),
            created_at=unit.get("created_at"),
        )

    def list(self, bank_id: str, *, tag_groups: tuple[TagGroup, ...], state: MemoryState | None,
             limit: int, offset: int) -> Page:
        """The engine's `list_memories` filters on one tag set; ACH's `tag_groups` can carry
        more than one. Only the first group goes server-side; any further group is applied here,
        on the one page already fetched.

        # ponytail: paging is then approximate when more than one group is given (`total` stays
        # the engine's count for the first group alone) -- exact paging needs the engine to
        # accept more than one tag group on this endpoint.
        """
        params: dict[str, Any] = {"type": "world", "state": state or "valid",
                                   "limit": limit, "offset": offset}
        if tag_groups:
            first = tag_groups[0]
            params["tags"] = list(first.get("tags") or [])
            params["tags_match"] = "all" if first.get("match") == "all" else "any"
        response = self._request("GET", f"{_bank(bank_id)}/memories/list", params=params)
        remaining_groups = tag_groups[1:]
        items: list[MemoryView] = []
        for raw in (response or {}).get("items") or []:
            tags = tuple(raw.get("tags") or ())
            if remaining_groups and not matches_tag_groups(tags, remaining_groups):
                continue
            metadata = dict(raw.get("metadata") or {})
            memory_id = metadata.get("memory_id") or raw.get("document_id")
            items.append(MemoryView(
                memory_id=memory_id, text=raw.get("text", ""),
                state=raw.get("state") or (state or "valid"), tags=tags,
                metadata=_public_metadata(metadata), created_at=raw.get("created_at"),
            ))
        return Page(items=tuple(items), total=(response or {}).get("total") or 0)

    def reflect(self, bank_id: str, query: str, *, tag_groups: tuple[TagGroup, ...]) -> str:
        translated = [_translate_tag_group(g) for g in tag_groups]
        body: dict[str, Any] = {"query": query}
        if translated:
            body["tag_groups"] = translated
        response = self._request("POST", f"{_bank(bank_id)}/reflect", body,
                                  timeout=self._llm_timeout)
        return (response or {}).get("text") or ""

    def get_operation(self, bank_id: str, operation_ref: str) -> dict:
        result = self._request("GET", f"{_bank(bank_id)}/operations/{operation_ref}",
                                not_found=NotFound)
        return _strip_bank_id(_derive_failed(result), bank_id)

    def list_operations(self, bank_id: str, *, status: str | None, limit: int,
                         offset: int) -> dict:
        response = self._request("GET", f"{_bank(bank_id)}/operations",
                                  params=_present({"status": status, "limit": limit,
                                                    "offset": offset}))
        return _strip_bank_id(response, bank_id)

    def cancel_operation(self, bank_id: str, operation_ref: str) -> dict:
        result = self._request("DELETE", f"{_bank(bank_id)}/operations/{operation_ref}",
                                not_found=NotFound)
        return _strip_bank_id(result, bank_id)

    def provision_mental_models(self, bank_id: str, builtins: Sequence[BuiltinModel]) -> None:
        path = f"{_bank(bank_id)}/mental-models"
        existing = {
            item.get("name"): item
            for item in (self._request("GET", path) or {}).get("items") or []
        }
        for builtin in builtins:
            body = {
                "name": builtin.name,
                "source_query": builtin.prompt,
                "tags": list(builtin.source_tags),
                "tags_match": builtin.tags_match,
            }
            current = existing.get(builtin.name)
            if current is None:
                self._request("POST", path, body)
            elif current.get("source_query") != builtin.prompt:
                self._request("PATCH", f"{path}/{current['id']}", body)

    def get_mental_model(self, bank_id: str, key: str) -> MentalModelView | None:
        """Hindsight indexes by name, not `key` -- resolved via BUILTIN_MODELS, the only keys ACH passes."""
        name = next((m.name for m in BUILTIN_MODELS if m.key == key), None)
        if name is None:
            return None
        path = f"{_bank(bank_id)}/mental-models"
        existing = {i.get("name"): i for i in (self._request("GET", path) or {}).get("items") or []}
        item = existing.get(name)
        if item is None:
            return None
        content = (self._request("GET", f"{path}/{item['id']}") or {}).get("content")
        return MentalModelView(key=key, name=name, content=content if isinstance(content, str) else None,
                                updated_at=item.get("updated_at"))
