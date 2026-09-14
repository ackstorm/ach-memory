from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from memory.config import get_settings


@lru_cache
def get_engine() -> Engine:
    # hide_parameters=True: an unhandled exception's str() would otherwise
    # leak bound parameters (e.g. bank_id) into the application log.
    return create_engine(
        get_settings().database_url, pool_pre_ping=True, hide_parameters=True
    )


@contextmanager
def session_scope() -> Iterator[Session]:
    db = sessionmaker(bind=get_engine(), expire_on_commit=False)()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
