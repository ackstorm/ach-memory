class DomainError(Exception):
    """Base for every error the API reports with a stable code (SPEC 3.5)."""

    code = "INTERNAL_ERROR"
    status = 500

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code


class InvalidRequest(DomainError):
    code = "INVALID_REQUEST"
    status = 400


class Unauthorized(DomainError):
    code = "UNAUTHORIZED"
    status = 401


class Forbidden(DomainError):
    code = "FORBIDDEN"
    status = 403


class NotFound(DomainError):
    code = "NOT_FOUND"
    status = 404


class MemoryNotFound(DomainError):
    code = "MEMORY_NOT_FOUND"
    status = 404


class ProjectNotFound(DomainError):
    code = "PROJECT_NOT_FOUND"
    status = 404


class ContentRejectedBySanitizer(DomainError):
    code = "CONTENT_REJECTED_BY_SANITIZER"
    status = 422


class UpstreamError(DomainError):
    """Adapter outcome unknown or failed; safe to retry with the same operation_id."""

    code = "UPSTREAM_ERROR"
    status = 502


class UnsupportedCapability(DomainError):
    code = "UNSUPPORTED"
    status = 501
