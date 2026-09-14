"""Guard for caller-supplied text reaching Postgres.

A NUL byte raises psycopg.DataError at parameter adaptation, not
IntegrityError -- so it must be screened before it ever hits a query.
"""


def has_control_character(value: str) -> bool:
    """True if `value` contains a C0 control character or DEL."""
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)
