"""One guard for caller-supplied identifiers that reach Postgres.

`memory.hindsight.paths._reject_path_traversal` already does this for ids
that reach a Hindsight URL. This is the same defence for the ids that reach
our OWN database: user_id, group_id, retired_slug and the audit filters.

Why it has to exist at all: psycopg refuses a NUL byte at parameter
adaptation with `psycopg.DataError`, which SQLAlchemy wraps as
`sqlalchemy.exc.DataError`. That is NOT an `IntegrityError`, so the
`except IntegrityError` guards around every insert in this service never see
it, and it reaches `api/app.py`'s catch-all as a 500 -- a caller mistake
reported as a backend fault, which SPEC §18 exists to prevent. Measured live
on eight routes (2026-08-23 review, R2-I2).

Raises the route's own not-found error, never a 400: a malformed id cannot
name anything that exists, and answering "not found" discloses nothing about
why it was rejected -- the same reasoning `_reject_path_traversal` documents.
"""

from memory.errors import DomainError


def _has_control_character(value: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def reject_control_characters(value: str | None, not_found: type[DomainError]) -> None:
    """Refuse an identifier Postgres cannot store.

    None and "" pass through untouched: absence is the caller's business and
    is handled by the route's own lookup, not by this guard. Used at lookup
    boundaries -- see `is_unstorable` for the filter case, where a value
    Postgres can't store should match nothing instead of raising.
    """
    if not value:
        return
    if _has_control_character(value):
        raise not_found("no such object")


def is_unstorable(value: str | None) -> bool:
    """True if Postgres would refuse `value` as a parameter.

    For a FILTER (narrows a set) rather than a lookup (names one object): an
    unstorable value can't match any row, so the honest answer is an empty
    result, not the 500 psycopg would otherwise raise at parameter
    adaptation. See admin.list_audit.
    """
    return bool(value) and _has_control_character(value)
