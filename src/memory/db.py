from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from memory.config import get_settings
from memory.models import Tenant


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def ensure_tenant(db: Session, tenant_id: str) -> None:
    """Create the tenant row on first use. Mono-tenant in v1, so this fires
    once per deployment and then never again.

    The savepoint matches `create_user`, `create_group` and `projects.create`:
    two provisioning calls racing on a fresh tenant both saw None, both
    inserted, and the loser's IntegrityError escaped as a bare 500 (review
    finding I9). Losing the race is success here -- the row exists either way.
    """
    if db.get(Tenant, tenant_id) is not None:
        return
    try:
        with db.begin_nested():
            db.add(Tenant(id=tenant_id))
    except IntegrityError:
        pass


def get_session() -> Iterator[Session]:
    """FastAPI dependency. Rolls back on exception; never commits.

    Committing in the teardown would run AFTER the response has been sent, so a
    failed commit could not be reported to a caller who already holds, say, a
    plaintext API key. Write handlers commit explicitly, before they return.
    """
    db = _session_factory()()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# A Session outside the FastAPI request cycle, for MCP tools -- the exact same
# body as get_session (same rollback/never-commit discipline documented
# there), just wrapped so `with session_scope() as db:` works where there is
# no FastAPI dependency injector to call it for you.
session_scope = contextmanager(get_session)
