import logging
import time
import uuid
from functools import lru_cache
from typing import Any

import httpx

from memory import metrics
from memory.config import get_settings
from memory.errors import (
    DirectiveNotFound,
    DocumentNotFound,
    DomainError,
    HindsightError,
    MemoryNotCuratable,
    MemoryNotFound,
    MentalModelNotFound,
    OperationNotCancellable,
    OperationNotFound,
    UpstreamRejected,
)
from memory.hindsight import paths

logger = logging.getLogger("memory.hindsight")

# The two states we have seen Hindsight hang in. An allowlist, not a
# denylist of terminal states, on purpose: if a future Hindsight adds a
# status this code has never seen, it simply gets no derived failure. That
# direction is safe -- the caller sees upstream's own status untouched -- and
# the alternative would have us overwrite a state whose semantics we do not
# know.
_NON_TERMINAL = ("pending", "running")


def _derive_failed(record: dict) -> dict:
    """Report `failed` for an operation whose every child already errored.

    Hindsight 0.9.1 never transitions the parent: measured live 2026-08-22, an
    async retain with a failed child sat at `pending` through 30s of polling,
    the reason readable only in child_operations[N].error_message. A caller
    polling for a terminal status would wait forever, which is how a silently
    lost write looks from the outside.

    Deliberately narrow: only when the parent is non-terminal AND there is at
    least one child AND every child carries an error_message. A partial
    failure leaves the status alone -- work may still be in flight, and
    stopping a caller's poll early would lose the rest of it.
    """
    children = record.get("child_operations") or []
    if record.get("status") not in _NON_TERMINAL or not children:
        return record
    if all(child.get("error_message") for child in children):
        return {**record, "status": "failed"}
    return record


def _present(values: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the caller left unset.

    Sending `state=None` as a query parameter is not the same as omitting it —
    httpx renders it as an empty string and Hindsight filters on that.
    """
    return {k: v for k, v in values.items() if v is not None}


def _require_uuid(value: str, not_found: type[DomainError]) -> None:
    """Reject an id that cannot exist upstream, before the round trip.

    memory_id and operation_id are UUIDs upstream (Hindsight's retain schema
    describes operation_id as a "client-supplied UUID"); a malformed one is a
    400 from Hindsight, not a 404 -- blaming the backend for bad caller input.
    An id that fails this check honestly cannot exist, so raising the same
    not-found error the 404 branch raises is correct and saves a round trip.

    document_id is caller-managed and arbitrary (e.g.
    "github:acme/api:pr:382") and must NEVER be checked this way.
    """
    malformed = False
    try:
        uuid.UUID(value)
    except ValueError:
        malformed = True

    if malformed:
        # Raised outside the except for the same reason _request does it: an
        # exception raised inside one keeps the original on __context__, and
        # this file's rule is that nothing walks out of here with a chain.
        raise not_found("no such object in this memory")


class HindsightClient:
    def __init__(self, base_url: str, api_key: str, tenant_id: str) -> None:
        # Threaded through every paths.* call below for signature stability,
        # but paths.bank() ignores it and always emits HINDSIGHT_TENANT
        # ("default") -- hindsight-api 0.9.1 hardcodes that segment in all 83
        # bank routes and resolves its own tenancy from the Authorization
        # header, never the URL (review finding I4). MEMORY_TENANT_ID still
        # drives our own DB tenancy; it just no longer reaches Hindsight.
        self._tenant = tenant_id
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        settings = get_settings()
        # Connect stays short whatever the call is -- an unreachable backend
        # must fail fast. Only the READ side is extended, and only for the two
        # calls that actually wait on a model.
        self._default_timeout = httpx.Timeout(
            settings.hindsight_timeout_seconds, connect=5.0
        )
        self._llm_timeout = httpx.Timeout(
            settings.hindsight_llm_timeout_seconds, connect=5.0
        )
        self._http = httpx.Client(
            base_url=base_url, headers=headers, timeout=self._default_timeout
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
        not_found: type[DomainError] | None = None,
        bad_request: type[DomainError] | None = None,
        conflict: type[DomainError] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> dict:
        response = None
        started = time.monotonic()
        # "error" until proven otherwise: a transport failure never yields a
        # status code, and it is the case an operator most needs to see.
        upstream_status = "error"
        try:
            response = self._http.request(
                method, path, json=payload, params=params,
                timeout=timeout or self._default_timeout,
            )
            upstream_status = str(response.status_code)
        except httpx.HTTPError as exc:
            # Logged here and never attached to the raised error: the httpx
            # exception holds .request.url, which contains the bank ID.
            logger.warning("hindsight transport failure: %s", type(exc).__name__)
        finally:
            metrics.HINDSIGHT.labels(method=method, status=upstream_status).observe(
                time.monotonic() - started
            )

        if response is None:
            # Raised OUTSIDE the except block on purpose. Inside it, Python sets
            # __context__ to the httpx error even with `from None`, and anything
            # walking __context__ would reach the bank ID.
            raise HindsightError("memory backend unreachable")

        if response.status_code == 404 and not_found is not None:
            # A secondary resource that is absent from an ALREADY-AUTHORIZED
            # bank is an ordinary 404, not a backend fault. The upstream body is
            # never echoed: it can name the bank.
            #
            # Logged here, naming only the error class: a wrong tenant_id, a
            # renamed path (Hindsight upgrade), and a genuine miss are all the
            # same 404 on the wire. Without this line a misconfiguration makes
            # every read silently report "absent" forever, to every caller.
            logger.warning("hindsight 404 mapped to %s", not_found.__name__)
            raise not_found("no such object in this memory")

        if response.status_code == 400 and bad_request is not None:
            # An upstream 400 means the caller's request was wrong, not that
            # the backend is unwell. Folding it into HINDSIGHT_ERROR tells an
            # agent to retry something that can never succeed. The upstream
            # body is still never echoed -- it can name the bank.
            logger.warning("hindsight 400 mapped to %s", bad_request.__name__)
            raise bad_request("this memory cannot be curated")

        if response.status_code == 409 and conflict is not None:
            # A terminal operation cannot be cancelled. This is route-specific:
            # other upstream conflicts retain their existing HINDSIGHT_ERROR
            # mapping. The upstream body can name the bank or operation, so it
            # is neither returned nor attached to the domain error.
            logger.warning("hindsight 409 mapped to %s", conflict.__name__)
            raise conflict("the operation is no longer cancellable")

        if response.status_code == 422:
            # The upstream is FastAPI: a schema violation is a 422, never a
            # 400. Folding it into HINDSIGHT_ERROR told an agent to retry a
            # request that can never succeed (review finding I6). The body is
            # still never echoed -- it can name the bank.
            logger.warning("hindsight 422: request shape rejected upstream")
            raise UpstreamRejected("the memory backend rejected this request shape")

        if response.status_code >= 400:
            # upstream_status is deliberately NOT in details: an upstream
            # 401/403 means OUR MEMORY_HINDSIGHT_API_KEY is misconfigured, and
            # the backend's auth state is not an untrusted MCP caller's
            # business. Logged instead, where an operator can see it.
            logger.warning("hindsight rejected the request: %s", response.status_code)
            raise HindsightError("memory backend rejected the request")

        if response.status_code >= 300:
            # follow_redirects is False by default, so a trailing-slash
            # redirect arrives here as a bodiless 3xx. Treating it as success
            # meant .json() raised JSONDecodeError -- which is NOT an
            # httpx.HTTPError, so it walked past the handler above and became
            # INTERNAL_ERROR instead of a typed backend failure.
            logger.warning("hindsight redirected: %s", response.status_code)
            raise HindsightError("memory backend rejected the request")

        if not response.content:
            # 204 No Content is a legitimate success for a DELETE. An empty
            # body is an empty result, not a parse failure.
            return {}

        try:
            return response.json()
        except ValueError:
            # An HTML error page from an intermediary, or any other non-JSON
            # 2xx. Same reasoning as the 3xx branch: a decode failure is a
            # backend problem and must arrive as HINDSIGHT_ERROR, not as the
            # catch-all's INTERNAL_ERROR.
            logger.warning("hindsight returned a non-JSON success body")
            raise HindsightError(
                "memory backend returned an unreadable response"
            ) from None

    def retain(
        self,
        bank_id: str,
        content: str,
        *,
        document_id: str | None = None,
        metadata: dict[str, str] | None = None,
        context: str | None = None,
        update_mode: str = "replace",
        is_async: bool = True,
        operation_id: str | None = None,
    ) -> dict:
        item: dict[str, Any] = {"content": content, "update_mode": update_mode}
        if document_id is not None:
            # Applied here too, not just at get/delete's URL boundary: a
            # document_id that get_document/delete_document would refuse
            # (".", "..", a leading "/", control characters, ...) must not be
            # writable either, or the document it names is created once and
            # unreachable forever after (SPEC §12.2's hard-delete lever
            # defeated by a name nothing can address again).
            paths.reject_document_traversal(document_id)
            item["document_id"] = document_id
        if metadata:
            item["metadata"] = metadata
        if context is not None:
            item["context"] = context

        # No "tags" key is ever sent: v1 writes no retrieval tags (SPEC §13.6).
        body: dict[str, Any] = {"items": [item], "async": is_async}
        if operation_id is not None:
            # Top-level on RetainRequest, not per-item: it identifies the
            # whole async operation.
            body["operation_id"] = operation_id
        return self._request(
            "POST", paths.retain(self._tenant, bank_id), body,
            # Synchronous retain blocks on the extraction LLM; the async form
            # returns an operation immediately and needs no extra headroom.
            timeout=None if is_async else self._llm_timeout,
        )

    def recall(self, bank_id: str, query: str) -> dict:
        return self._request(
            "POST", paths.recall(self._tenant, bank_id), {"query": query}
        )

    def reflect(self, bank_id: str, query: str) -> dict:
        return self._request(
            "POST", paths.reflect(self._tenant, bank_id), {"query": query},
            timeout=self._llm_timeout,  # a full synthesis call
        )

    def list_memories(self, bank_id: str, **filters: Any) -> dict:
        return self._request(
            "GET",
            paths.memory_list(self._tenant, bank_id),
            params=_present(filters),
        )

    def get_memory(self, bank_id: str, memory_id: str) -> dict:
        _require_uuid(memory_id, MemoryNotFound)
        return self._request(
            "GET",
            paths.memory(self._tenant, bank_id, memory_id),
            not_found=MemoryNotFound,
        )

    def curate(
        self,
        bank_id: str,
        memory_id: str,
        *,
        text: str | None = None,
        state: str | None = None,
        reason: str | None = None,
    ) -> dict:
        """One PATCH, three meanings (SPEC §12).

        state="invalidated" is forget, state="valid" is restore, text=... is
        correct. Hindsight has no DELETE for a memory; it is append-only, and
        invalidation is reversible on purpose.
        """
        _require_uuid(memory_id, MemoryNotFound)
        body = _present({"text": text, "state": state, "reason": reason})
        return self._request(
            "PATCH",
            paths.memory(self._tenant, bank_id, memory_id),
            body,
            not_found=MemoryNotFound,
            # Hindsight 400s a curate on a derived `observation`. That is a
            # property of the memory the caller named, not a backend fault, so
            # it must not become a 502 -- see MemoryNotCuratable.
            bad_request=MemoryNotCuratable,
        )

    def list_documents(self, bank_id: str, **filters: Any) -> dict:
        return self._request(
            "GET", paths.documents(self._tenant, bank_id), params=_present(filters)
        )

    def get_document(self, bank_id: str, document_id: str) -> dict:
        return self._request(
            "GET",
            paths.document(self._tenant, bank_id, document_id),
            not_found=DocumentNotFound,
        )

    def delete_document(self, bank_id: str, document_id: str) -> dict:
        return self._request(
            "DELETE",
            paths.document(self._tenant, bank_id, document_id),
            not_found=DocumentNotFound,
        )

    def get_operation(self, bank_id: str, operation_id: str) -> dict:
        _require_uuid(operation_id, OperationNotFound)
        result = self._request(
            "GET",
            paths.operation(self._tenant, bank_id, operation_id),
            not_found=OperationNotFound,
        )
        # Measured live: an absent operation is a 200 with
        # {"status": "not_found"}, never a 404 -- the `not_found=` mapping
        # above never actually fires for this route. `cancel_operation`
        # below is unaffected: DELETE on an absent operation IS a real 404.
        if result.get("status") == "not_found":
            raise OperationNotFound("no such object in this memory")
        return _derive_failed(result)

    def list_operations(self, bank_id: str, **filters: Any) -> dict:
        return self._request(
            "GET", paths.operations(self._tenant, bank_id), params=_present(filters)
        )

    def cancel_operation(self, bank_id: str, operation_id: str) -> dict:
        _require_uuid(operation_id, OperationNotFound)
        return self._request(
            "DELETE",
            paths.operation(self._tenant, bank_id, operation_id),
            not_found=OperationNotFound,
            conflict=OperationNotCancellable,
        )

    def create_directive(
        self,
        bank_id: str,
        *,
        name: str,
        content: str,
        priority: int | None = None,
        is_active: bool | None = None,
    ) -> dict:
        # No "tags" key is ever sent: it is Hindsight's in-bank visibility
        # scope, a dimension this service does not model (SPEC §14, §13.6).
        body = _present(
            {"name": name, "content": content, "priority": priority, "is_active": is_active}
        )
        return self._request("POST", paths.directives(self._tenant, bank_id), body)

    def list_directives(
        self,
        bank_id: str,
        *,
        active_only: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict:
        return self._request(
            "GET",
            paths.directives(self._tenant, bank_id),
            params=_present({"active_only": active_only, "limit": limit, "offset": offset}),
        )

    def get_directive(self, bank_id: str, directive_id: str) -> dict:
        _require_uuid(directive_id, DirectiveNotFound)
        return self._request(
            "GET",
            paths.directive(self._tenant, bank_id, directive_id),
            not_found=DirectiveNotFound,
        )

    def update_directive(
        self,
        bank_id: str,
        directive_id: str,
        *,
        name: str | None = None,
        content: str | None = None,
        priority: int | None = None,
        is_active: bool | None = None,
    ) -> dict:
        _require_uuid(directive_id, DirectiveNotFound)
        body = _present(
            {"name": name, "content": content, "priority": priority, "is_active": is_active}
        )
        return self._request(
            "PATCH",
            paths.directive(self._tenant, bank_id, directive_id),
            body,
            not_found=DirectiveNotFound,
        )

    def delete_directive(self, bank_id: str, directive_id: str) -> dict:
        _require_uuid(directive_id, DirectiveNotFound)
        return self._request(
            "DELETE",
            paths.directive(self._tenant, bank_id, directive_id),
            not_found=DirectiveNotFound,
        )

    def create_mental_model(
        self,
        bank_id: str,
        *,
        name: str,
        source_query: str,
        max_tokens: int | None = None,
        trigger: dict[str, Any] | None = None,
    ) -> dict:
        # trigger is passed through verbatim when supplied and omitted
        # entirely when not (SPEC §14.5) -- `_present` already drops a None
        # value, so an omitted trigger sends no "trigger" key at all, never
        # `{}` or a default. No shape validation, no default of our own: a
        # model created without one performs no automatic refresh, which is
        # Hindsight's own cheapest and safest behavior. tags is never sent,
        # same reasoning as create_directive.
        body = _present(
            {
                "name": name,
                "source_query": source_query,
                "max_tokens": max_tokens,
                "trigger": trigger,
            }
        )
        return self._request("POST", paths.mental_models(self._tenant, bank_id), body)

    def list_mental_models(
        self,
        bank_id: str,
        *,
        detail: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict:
        return self._request(
            "GET",
            paths.mental_models(self._tenant, bank_id),
            params=_present({"detail": detail, "limit": limit, "offset": offset}),
        )

    def get_mental_model(self, bank_id: str, mental_model_id: str) -> dict:
        paths.reject_mental_model_id_traversal(mental_model_id)
        return self._request(
            "GET",
            paths.mental_model(self._tenant, bank_id, mental_model_id),
            not_found=MentalModelNotFound,
        )

    def update_mental_model(
        self,
        bank_id: str,
        mental_model_id: str,
        *,
        name: str | None = None,
        source_query: str | None = None,
        max_tokens: int | None = None,
        trigger: dict[str, Any] | None = None,
    ) -> dict:
        paths.reject_mental_model_id_traversal(mental_model_id)
        body = _present(
            {
                "name": name,
                "source_query": source_query,
                "max_tokens": max_tokens,
                "trigger": trigger,
            }
        )
        return self._request(
            "PATCH",
            paths.mental_model(self._tenant, bank_id, mental_model_id),
            body,
            not_found=MentalModelNotFound,
        )

    def delete_mental_model(self, bank_id: str, mental_model_id: str) -> dict:
        paths.reject_mental_model_id_traversal(mental_model_id)
        return self._request(
            "DELETE",
            paths.mental_model(self._tenant, bank_id, mental_model_id),
            not_found=MentalModelNotFound,
        )

    def refresh_mental_model(self, bank_id: str, mental_model_id: str) -> dict:
        # Never dry-run-refresh (SPEC §11.7): it costs exactly the same as a
        # real refresh, so there is no cheap variant to wire on any surface.
        paths.reject_mental_model_id_traversal(mental_model_id)
        return self._request(
            "POST",
            paths.mental_model_refresh(self._tenant, bank_id, mental_model_id),
            not_found=MentalModelNotFound,
        )

    def clear_mental_model(self, bank_id: str, mental_model_id: str) -> dict:
        paths.reject_mental_model_id_traversal(mental_model_id)
        return self._request(
            "POST",
            paths.mental_model_clear(self._tenant, bank_id, mental_model_id),
            not_found=MentalModelNotFound,
        )

    def clear_memories(self, bank_id: str, *, type: str | None = None) -> dict:
        """SPEC §11.7: admin API + master key only, never advertised over MCP
        -- "an LLM that decides memory is 'stale' will use them." Irreversible,
        unlike curate()'s invalidate. `type` narrows to one fact type
        (world/experience/observation); omitted, it clears the whole bank.
        """
        return self._request(
            "DELETE",
            paths.clear_memories(self._tenant, bank_id),
            params=_present({"type": type}),
        )

    def delete_bank(self, bank_id: str) -> dict:
        """SPEC §11.7 + §12.3: irreversible, whole-bank. For scope=user this
        is the only complete right-to-erasure path for a departing user.

        A plain DELETE now. There is no cache to evict: `ensure_bank` no
        longer holds any per-bank state, and there is no config PATCH left
        for a stale entry to wrongly skip (Plan 6 Task 1).
        """
        return self._request("DELETE", paths.bank(self._tenant, bank_id))

    def ensure_bank(self, bank_id: str) -> None:
        """Create the bank upstream. Idempotent, and cheap enough to repeat.

        Only `create_directive` needs this. Banks auto-create on first use --
        measured live: POST to `.../banks/<never-created>/memories` returns
        200 -- so retain, recall, reflect and create_mental_model must NOT pay
        for this round trip. A directive POST on a bank nothing has ever
        retained into 500s upstream (also measured), which is the one case
        left that needs the row to exist first.

        No config PATCH: as of Plan 6 there is nothing to configure.
        `memory_defense` was accepted and enforced-by-nothing in
        hindsight-api 0.9.1 (SPEC §20.2) and screening moved to the LiteLLM
        `pre_mcp_call` guardrail outside this service;
        `store_document_text` now stays at Hindsight's default (True, verified
        via `DEFAULT_STORE_DOCUMENT_TEXT`) so `update_mode="append"` works
        (SPEC §11.4) -- setting it explicitly would send the default back.

        No TTL cache either. It existed to skip that PATCH, and it was per
        process: with replicaCount>1 a `delete_bank` served by pod B left pod
        A's entry live, and pod A then skipped re-materialization.
        """
        self._request("PUT", paths.bank(self._tenant, bank_id), {})


@lru_cache
def get_client() -> HindsightClient:
    settings = get_settings()
    return HindsightClient(
        base_url=settings.hindsight_url,
        api_key=settings.hindsight_api_key,
        tenant_id=settings.tenant_id,
    )
