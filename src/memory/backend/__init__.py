from functools import lru_cache

from memory.backend.base import Backend, Hit, MemoryView, Page, TagGroup, WriteAck  # re-exports

__all__ = ["Backend", "Hit", "MemoryView", "Page", "TagGroup", "WriteAck", "get_backend"]


@lru_cache(maxsize=1)
def get_backend() -> Backend:
    from memory.config import get_settings

    settings = get_settings()
    if settings.backend == "fake":
        from memory.backend.fake import fake_backend

        return fake_backend
    from memory.backend.hindsight import HindsightBackend

    return HindsightBackend()
