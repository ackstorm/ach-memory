"""Turn a verified external identity into a local user, on first sight."""

import hashlib

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory.models import ExternalIdentity, User

USER_PREFIX = "usr_"


def _user_id_for(issuer: str, subject: str) -> str:
    """Mint the id for an identity seen for the first time.

    Separated by a newline, which neither an issuer URL nor a subject can
    contain. A ":" separator does not survive an issuer URL, which is full of
    them: ("a:b", "c") and ("a", "b:c") both join to "a:b:c" and would mint one
    user id for two identities -- the second then dies on the users PK forever.

    Only ever called for an identity with no `external_identities` row, so
    changing how it joins cannot move a user that already exists.
    """
    digest = hashlib.sha256(f"{issuer}\n{subject}".encode()).hexdigest()
    return f"{USER_PREFIX}{digest[:32]}"


def _user_for(db: Session, row: ExternalIdentity) -> User:
    user = db.get(User, row.user_id)
    assert user is not None
    return user


def link_identity(db: Session, *, issuer: str, subject: str) -> User:
    """Return the `User` for an external identity, creating it on first sight.

    Deterministic ids mean two concurrent first requests for the same
    identity compute the same row rather than racing to mint one: the loser
    of the `IntegrityError` just reloads what the winner committed.

    Commits explicitly, and must: `session_scope` does not commit on its own,
    and a read-only first contact (e.g. a `recall`) would otherwise roll this
    row back while the response went out 200 -- the caller then arrives with
    a "new" identity, and therefore a new bank, on every subsequent request.
    """
    row = db.get(ExternalIdentity, (issuer, subject))
    if row is not None:
        return _user_for(db, row)

    user_id = _user_id_for(issuer, subject)
    user = User(id=user_id, bank_id=f"user_{user_id}")
    try:
        with db.begin_nested():
            db.add(user)
            # No relationship() means no dependency edge to order by, so
            # flush user first or the FK insert races ahead of it.
            db.flush()
            db.add(
                ExternalIdentity(issuer=issuer, subject=subject, user_id=user_id)
            )
    except IntegrityError:
        row = db.get(ExternalIdentity, (issuer, subject))
        if row is None:
            raise
        return _user_for(db, row)

    db.commit()
    return user
