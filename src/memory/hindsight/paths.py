"""Hindsight endpoint paths.

Pinned against the openapi.json of the deployed Hindsight version. These are
the first thing to re-check on a Hindsight upgrade.

NOTE: `retain` is pinned to the discovered `/memories` path (POST "Retain
memories" in openapi.json), not the `/memory/retain` path guessed from
Hindsight's (self-contradictory) published docs -- the discovered value wins.
"""

from urllib.parse import unquote

from memory.errors import DocumentNotFound, DomainError, MentalModelNotFound


def _reject_path_traversal(value: str, not_found: type[DomainError]) -> None:
    """Refuse a value that httpx would not treat as one opaque path segment.

    Shared by `reject_document_traversal` (document_id, caller-managed and
    arbitrary) and `reject_mental_model_id_traversal` (mental_model_id,
    Hindsight-minted as `mm-<32 hex>`, NOT a UUID -- see that function's own
    docstring for why `_require_uuid` was wrong for it). Both ids reach a
    Hindsight URL path, so both need the same defense; only the exception
    raised on rejection differs.

    httpx applies RFC 3986 dot-segment removal when it merges a path onto
    `base_url`, so an unvalidated value of ".." resolves ONE LEVEL UP from
    the segment's own parent -- for a document that lands on the bank itself
    (`DELETE` there is `delete_bank`, not `delete_document`); for a mental
    model it lands on `.../mental-models`, the collection route. A longer
    traversal ("../../../../../v1/default/banks/OTHER/memories") escapes the
    bank entirely and lands on another tenant's bank, or on `clear_memories`.
    Both are off this surface by design (SPEC §11.7): admin API + master key
    only. A bare `?`/`#` is just as dangerous -- it turns the rest of the id
    into a query string or fragment, silently dropping part of what was meant
    to be the path.

    A control character (`\r`, `\n`, `\t`, ...) is rejected too: httpx raises
    `InvalidURL` while merging a path segment containing one, and that
    exception does NOT inherit `httpx.HTTPError` -- it walks straight past
    `HindsightClient._request`'s handler to the app's catch-all, surfacing as
    a 500 INTERNAL_ERROR (not in SPEC §18's closed list of error codes)
    instead of the ordinary refusal every other malformed id gets here.

    Raising `not_found` (never a 400) makes the refusal indistinguishable
    from an absent object: it discloses nothing about why the id was
    rejected.

    The invariant is evaluated after each percent-decoding layer, until it
    reaches a fixed point: no layer may contain a leading slash or backslash,
    a backslash, `?`/`#`, a dot segment, or a control character.  Literal
    slashes remain valid document-id syntax, but an encoded slash is refused
    before it can become a new path delimiter.  A literal `%` (including a
    malformed or harmless escape) remains valid unless a later decoded layer
    creates one of those unsafe forms.
    """
    candidate = value
    while True:
        if not candidate or candidate.startswith(("/", "\\")):
            raise not_found("no such object in this memory")
        if "\\" in candidate or "?" in candidate or "#" in candidate:
            raise not_found("no such object in this memory")
        if any(segment in (".", "..") for segment in candidate.split("/")):
            raise not_found("no such object in this memory")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in candidate):
            raise not_found("no such object in this memory")

        decoded = unquote(candidate)
        if decoded == candidate:
            return

        # Literal `/` remains supported in caller-managed document ids, but a
        # percent-decoded slash is newly introduced delimiter syntax.  Reject
        # it before moving to the next decoding layer; `\\` is rejected above
        # once decoded for the same reason.
        if "%2f" in candidate.lower():
            raise not_found("no such object in this memory")
        candidate = decoded


def reject_document_traversal(document_id: str) -> None:
    """Refuse a `document_id` that httpx would not treat as an opaque segment.

    Public (not `_`-prefixed) because two call sites need it: `document()`
    below, for get/delete, and `HindsightClient.retain()`, at the WRITE
    boundary. Retain's document_id never reaches a URL path (it rides in the
    JSON body), so this guard's original httpx-traversal rationale does not
    apply there -- but a document written past this check would still be
    unreachable forever afterward, since get/delete both refuse the same id.
    A document that cannot later be addressed must not be creatable.

    document_id is caller-managed and arbitrary (SPEC §11.4) so it is NEVER
    UUID-validated like memory_id/operation_id -- but it must still resolve
    to exactly one opaque path segment sequence inside `.../documents/`.
    """
    _reject_path_traversal(document_id, DocumentNotFound)


def reject_mental_model_id_traversal(mental_model_id: str) -> None:
    """Refuse a `mental_model_id` that httpx would not treat as an opaque
    segment.

    mental_model_id is Hindsight-minted as `mm-<32 hex>` (measured live,
    hindsight-api 0.9.1, 2026-08-22) -- it is NOT a UUID, so
    `HindsightClient._require_uuid` rejected every real one before the round
    trip ever happened (`get`/`update`/`delete`/`refresh`/`clear` all 404'd
    locally, forever -- only `create`/`list` worked). Unlike a genuine UUID
    id (memory_id, operation_id, directive_id), a shape check here would be
    both wrong (rejects real ids) and insufficient by itself (does nothing
    for a caller who never validates and just forwards whatever string it
    has) -- the actual hazard, same as document_id, is the id reaching a
    Hindsight URL path unescaped. This is the traversal/charset guard that
    replaces the wrong UUID check.
    """
    _reject_path_traversal(mental_model_id, MentalModelNotFound)


# hindsight-api 0.9.1 registers all 83 bank routes under the literal segment
# `default`; multi-tenancy upstream is resolved from the Authorization header
# into a Postgres schema, never from the URL. The `tenant` parameter is kept
# so the signature does not churn across the 20-odd helpers built on bank(),
# and so this constant is the single place to change if that ever moves.
HINDSIGHT_TENANT = "default"


def bank(tenant: str, bank_id: str) -> str:
    return f"/v1/{HINDSIGHT_TENANT}/banks/{bank_id}"


def retain(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/memories"


def clear_memories(tenant: str, bank_id: str) -> str:
    """Same path as retain(), opposite verb: DELETE wipes the bank (or one
    fact type via `?type=`), POST retains. Admin API + master key only
    (SPEC §11.7) -- never advertised over MCP."""
    return retain(tenant, bank_id)


def recall(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/memories/recall"


def reflect(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/reflect"


def memory_list(tenant: str, bank_id: str) -> str:
    # NOT `/memories`: that is retain (POST) and clear_memories (DELETE).
    return f"{bank(tenant, bank_id)}/memories/list"


def memory(tenant: str, bank_id: str, memory_id: str) -> str:
    return f"{bank(tenant, bank_id)}/memories/{memory_id}"


def documents(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/documents"


def document(tenant: str, bank_id: str, document_id: str) -> str:
    reject_document_traversal(document_id)
    return f"{bank(tenant, bank_id)}/documents/{document_id}"


def operations(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/operations"


def operation(tenant: str, bank_id: str, operation_id: str) -> str:
    # DELETE on this path CANCELS. `{path}/delete` removes a terminal
    # operation and is deliberately not exposed in v1 (SPEC §11.5).
    return f"{bank(tenant, bank_id)}/operations/{operation_id}"


def directives(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/directives"


def directive(tenant: str, bank_id: str, directive_id: str) -> str:
    return f"{directives(tenant, bank_id)}/{directive_id}"


def mental_models(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/mental-models"


def mental_model(tenant: str, bank_id: str, mental_model_id: str) -> str:
    return f"{mental_models(tenant, bank_id)}/{mental_model_id}"


def mental_model_refresh(tenant: str, bank_id: str, mental_model_id: str) -> str:
    # `$`-anchored by callers, not here: `.../mental-models`,
    # `.../mental-models/{id}` and this path all overlap under an unanchored
    # regex, which is exactly the mock trap the task brief calls out.
    return f"{mental_model(tenant, bank_id, mental_model_id)}/refresh"


def mental_model_clear(tenant: str, bank_id: str, mental_model_id: str) -> str:
    return f"{mental_model(tenant, bank_id, mental_model_id)}/clear"
