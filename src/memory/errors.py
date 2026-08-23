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


class ProjectInvalidSlug(DomainError):
    code = "PROJECT_INVALID_SLUG"
    status = 400


class GroupNotFound(DomainError):
    code = "GROUP_NOT_FOUND"
    status = 404


class GroupAlreadyExists(DomainError):
    code = "GROUP_ALREADY_EXISTS"
    status = 409


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


class RateLimited(DomainError):
    code = "RATE_LIMITED"
    status = 429


class RetiredSlugNotFound(DomainError):
    code = "RETIRED_SLUG_NOT_FOUND"
    status = 404


class KeyNotFound(DomainError):
    code = "KEY_NOT_FOUND"
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
