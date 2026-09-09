class DomainError(Exception):
    """Base for every error the API reports with a stable code (SPEC §18)."""

    code = "INTERNAL_ERROR"
    status = 500

    def __init__(self, message: str = "", **details: object) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details


class Unauthorized(DomainError):
    code = "UNAUTHORIZED"
    status = 401


class Forbidden(DomainError):
    code = "FORBIDDEN"
    status = 403


class InvalidScope(DomainError):
    code = "INVALID_SCOPE"
    status = 400


class ContentTooLarge(DomainError):
    code = "CONTENT_TOO_LARGE"
    status = 413


class HindsightError(DomainError):
    code = "HINDSIGHT_ERROR"
    status = 502


class AuthBackendUnavailable(DomainError):
    """The credential could not be checked, which is not the same as bad.

    Reporting a resolver outage as UNAUTHORIZED tells an agent its key is
    wrong -- so it stops retrying and a human starts rotating credentials --
    when the truth is that the check never ran. 503 says "ask again later",
    which is the only accurate thing we know.
    """

    code = "AUTH_BACKEND_UNAVAILABLE"
    status = 503


class ProjectInvalidSlug(DomainError):
    code = "PROJECT_INVALID_SLUG"
    status = 400


class GroupNotFound(DomainError):
    code = "GROUP_NOT_FOUND"
    status = 404


class UserNotFound(DomainError):
    code = "USER_NOT_FOUND"
    status = 404


class ProjectNotFound(DomainError):
    code = "PROJECT_NOT_FOUND"
    status = 404


class ProjectAccessDenied(DomainError):
    code = "PROJECT_ACCESS_DENIED"
    status = 403


class ProjectSlugConflict(DomainError):
    code = "PROJECT_SLUG_CONFLICT"
    status = 409


class ProjectLocatorMismatch(DomainError):
    code = "PROJECT_LOCATOR_MISMATCH"
    status = 409


class ProjectContextUnavailable(DomainError):
    code = "PROJECT_CONTEXT_UNAVAILABLE"
    status = 400


class InvalidOwnerType(DomainError):
    code = "INVALID_OWNER_TYPE"
    status = 400


class InvalidMetadata(DomainError):
    code = "INVALID_METADATA"
    status = 400


class InvalidTag(DomainError):
    code = "INVALID_TAG"
    status = 400


class MemoryNotFound(DomainError):
    code = "MEMORY_NOT_FOUND"
    status = 404


class MemoryNotCuratable(DomainError):
    """Hindsight refuses to curate a derived memory, and it is right to.

    An `observation` is synthesized from other facts and regenerates from
    them, so invalidating or editing one changes nothing that lasts -- the
    upstream message is "only world/experience facts can be curated". That is
    a fact about the memory the caller named, not a backend failure, so it
    must not arrive as HINDSIGHT_ERROR: a 502 tells an agent to retry, and
    retrying will never work.
    """

    code = "MEMORY_NOT_CURATABLE"
    status = 409


class DocumentNotFound(DomainError):
    code = "DOCUMENT_NOT_FOUND"
    status = 404


class OperationNotFound(DomainError):
    code = "OPERATION_NOT_FOUND"
    status = 404


class OperationNotCancellable(DomainError):
    code = "OPERATION_NOT_CANCELLABLE"
    status = 409


class RateLimited(DomainError):
    code = "RATE_LIMITED"
    status = 429


class RetiredSlugNotFound(DomainError):
    code = "RETIRED_SLUG_NOT_FOUND"
    status = 404


class DirectiveNotFound(DomainError):
    code = "DIRECTIVE_NOT_FOUND"
    status = 404


class MentalModelNotFound(DomainError):
    code = "MENTAL_MODEL_NOT_FOUND"
    status = 404


class UpstreamRejected(DomainError):
    """The upstream is FastAPI: a schema violation answers 422, never 400.

    Folding it into HINDSIGHT_ERROR told an agent to retry a request shape
    that can never succeed (review finding I6) -- distinct from
    MemoryNotCuratable, which is a fact about the memory named, not the
    request's shape.
    """

    code = "UPSTREAM_REJECTED"
    status = 400


class WorkingSessionNotFound(DomainError):
    """The (session_id, session_epoch) pair a write named does not resolve to
    a session this principal, project and workspace own.

    Covers both an invented epoch and a real epoch borrowed from someone
    else's session -- the server verifies the pair, so a caller can never win
    by inventing a large one. 404, not 409: an unrecognized session is not a
    fact about ordering, it names nothing at all.
    """

    code = "WORKING_SESSION_NOT_FOUND"
    status = 404


class WorkingStateStale(DomainError):
    """The write's (session_epoch, checkpoint_seq) pair is lexicographically
    behind the stored one. Working State keeps no history, so a stale write
    is simply refused rather than merged or queued."""

    code = "WORKING_STATE_STALE"
    status = 409


class WorkingStateConflict(DomainError):
    """The write's (session_epoch, checkpoint_seq) pair matches the stored
    one exactly but the payload differs. An identical retry at the same pair
    is idempotent; a different one at the same pair is a caller error, never
    last-write-wins."""

    code = "WORKING_STATE_CONFLICT"
    status = 409






class IdempotencyConflict(DomainError):
    code = "IDEMPOTENCY_CONFLICT"
    status = 409


class ContentRejectedBySanitizer(DomainError):
    code = "CONTENT_REJECTED_BY_SANITIZER"
    status = 422


class MentalModelQuotaExceeded(DomainError):
    code = "MODEL_QUOTA_EXCEEDED"
    status = 409


class BuiltinModelImmutable(DomainError):
    """A built-in's definition is versioned and ACH-owned, not caller-authored.

    SPEC §7.4: "immutable through custom-model CRUD... cannot be deleted
    while enabled as a built-in" -- the custom create/update/delete surface
    must refuse a built-in model_key rather than silently mutating it.
    """

    code = "BUILTIN_MODEL_IMMUTABLE"
    status = 409


class ContextBudgetExceeded(DomainError):
    """No longer a caller-triggerable error: standing delivery is built-ins
    only, so this can now only fire from `_create_builtin`/`_upgrade_builtin`
    when a `BuiltinModelDefinition`'s own `max_tokens` would exceed its
    scope's delivery budget -- an internal invariant over our two built-in
    definitions, not a mistake a REST or MCP caller can make."""

    code = "CONTEXT_BUDGET_EXCEEDED"
    status = 409


class InvalidValidityWindow(DomainError):
    """The database's own clock, not the application's, is authoritative for
    whether `valid_until` is still in the future (SPEC §5.8/§20 boundary):
    the same request can pass Pydantic's early check and still be stale by
    the time `accept_retain` reads database time under the bank lock."""

    code = "INVALID_VALIDITY_WINDOW"
    status = 422


class BankCurrentnessUnavailable(DomainError):
    code = "BANK_CURRENTNESS_UNAVAILABLE"
    status = 503


class CurationNeedsOperator(DomainError):
    code = "CURATION_NEEDS_OPERATOR"
    status = 503
