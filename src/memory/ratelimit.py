"""Per-credential write rate limiting (SPEC §20 MUST).

`check(principal)` is the module-level entry point every write path calls
through `memory.api.memory._resolve_bank`'s `is_write` flag (see that
function's docstring for why the check lives there and not at each route).
"""

import threading
import time
from collections import defaultdict, deque
from functools import lru_cache

from memory.auth.principal import Principal
from memory.config import get_settings
from memory.errors import RateLimited

# The master key has no `key_id` (SPEC §5.2 -- it is configuration, never a
# database row). Its traffic is ACH's own, not a human's, so every master-key
# call shares this one bucket rather than being unlimited by accident because
# `None` was never a value `Limiter.check` saw before.
MASTER_KEY_ID = "__master__"


class Limiter:
    """In-process, per-credential sliding window.

    Deliberately not Redis-backed. This bounds the runaway case that actually
    threatens us — one key looping retain or reflect — and needs no new
    infrastructure to do it. What it does NOT do: survive a restart, or
    coordinate across replicas. With N replicas the effective limit is N times
    the configured one. Say so before relying on it as a quota.
    """

    def __init__(self, limit: int, window_seconds: float, now=time.monotonic):
        self._limit = limit
        self._window = window_seconds
        self._now = now
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        # One lock for the whole map, not per key: this body is a few
        # microseconds of deque work with no I/O, so contention is not worth
        # per-key bookkeeping. Without it the read-check-append below is a
        # TOCTOU -- sync routes run in Starlette's threadpool and MCP tools in
        # AnyIO's 40-thread pool, so concurrent calls on one credential could
        # all read the same pre-append length and all pass (review finding
        # I8). This is separate from the per-replica multiplier documented on
        # the class.
        with self._lock:
            now = self._now()
            cutoff = now - self._window
            hits = self._hits[key]
            # Evict entries older than the window on every check, draining a
            # quiet key's deque back to empty. That does NOT free the key: the
            # `defaultdict` entry itself is never popped, so a credential that
            # calls once and goes silent forever still holds one empty deque for
            # the life of the process (measured: 100_000 one-shot credentials
            # retain 100_000 entries). Bounded in practice by the number of
            # credentials ever minted, not by traffic, so it is not chased here
            # -- but say so accurately rather than claim it self-frees.
            # `popleft()`, not `pop(0)`: O(1) on a deque vs O(n) on a list.
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._limit:
                raise RateLimited(
                    "too many writes for this credential",
                    retry_after_seconds=max(hits[0] + self._window - now, 0.0),
                )
            hits.append(now)


@lru_cache
def get_limiter() -> Limiter:
    settings = get_settings()
    return Limiter(settings.write_limit, settings.write_window_seconds)


def check(principal: Principal) -> None:
    """Rate-limit one write attributed to `principal`."""
    get_limiter().check(principal.key_id or MASTER_KEY_ID)
