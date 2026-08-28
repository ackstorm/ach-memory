"""The monotonic revision stamped on every compiled context snapshot.

Two channels deliver the brief -- the MCP proxy from a cache, the SessionStart
hook over HTTP -- so a consumer routinely holds two tiers that disagree by a
session. One revision per (user, project), stamped on both, is what lets it
tell which is newer without any reconciliation logic of its own.
"""

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory.models import ContextRevision


def fingerprint(*inputs: str | None) -> str:
    """A stable hash of every compiler input, so a bump means a real change.

    The harness never observes the nightly profile refresh directly; it sees
    the refreshed_at that comes back with the model. Hashing the inputs is
    what makes "bumped whenever any compiler input changes" implementable
    without a webhook.

    Joined on a unit separator rather than concatenated: ("ab", "c") and
    ("a", "bc") are different snapshots, and a bump that never happens is a
    stale tier with nothing marking it stale.
    """
    joined = "\x1f".join(value or "" for value in inputs)
    return hashlib.sha256(joined.encode()).hexdigest()


def current(
    db: Session, tenant_id: str, user_id: str, project_slug: str, digest: str
) -> int:
    """The revision for this snapshot, bumping it if the inputs moved.

    SELECT ... FOR UPDATE: two hosts starting a session at once would
    otherwise both bump, and the two tiers they cache would disagree by one
    forever. The insert path is the same race with no row to lock yet, so it
    goes through a savepoint and re-reads whoever won -- the pattern
    `db.ensure_tenant` uses, for the same reason (review finding I9). The
    re-read sees the winner because the row is committed by then and this runs
    at READ COMMITTED; two threads on a barrier between the SELECT and the
    INSERT pin both halves of that in tests.

    The caller commits: the revision a tier was stamped with must land in the
    same transaction as the read it describes, or a consumer can hold a
    revision this service never issued.
    """
    row = _locked(db, tenant_id, user_id, project_slug)
    now = datetime.now(UTC)

    if row is None:
        try:
            with db.begin_nested():
                db.add(
                    ContextRevision(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        project_slug=project_slug,
                        revision=1,
                        fingerprint=digest,
                        updated_at=now,
                    )
                )
            return 1
        except IntegrityError:
            # Lost the race; the winner's row is what this snapshot is
            # numbered by, exactly as if it had always been there.
            row = _locked(db, tenant_id, user_id, project_slug)
            if row is None:
                raise

    if row.fingerprint != digest:
        row.revision += 1
        row.fingerprint = digest
        row.updated_at = now
    return row.revision


def _locked(
    db: Session, tenant_id: str, user_id: str, project_slug: str
) -> ContextRevision | None:
    return db.execute(
        select(ContextRevision)
        .where(
            ContextRevision.tenant_id == tenant_id,
            ContextRevision.user_id == user_id,
            ContextRevision.project_slug == project_slug,
        )
        .with_for_update()
    ).scalar_one_or_none()
