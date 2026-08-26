"""One row per data-plane call, plus the metrics that go with it.

Two hooks cover both surfaces. `new_call()` runs at the edge (the REST
middleware, MCP's `_run`) and puts a MUTABLE dict in a ContextVar.
`describe()` runs deep inside the call -- in `_resolve_bank`, which every
REST route and all fifteen MCP tools already funnel through -- and MUTATES
that dict. `finish()` runs back at the edge and writes the row.

The mutation is load-bearing, not a style choice. Sync FastAPI routes run in
Starlette's threadpool and MCP tools in AnyIO's worker pool; both get a COPY
of the context, so a `ContextVar.set()` performed down there is invisible up
here. Passing the dict down and mutating it is what survives the boundary.
Rebind the var from inside a route and this module silently records nothing,
while every mock-level test still passes.

The row is written ONCE, at the end, never inserted-then-updated: one round
trip, and a row can never claim a retain succeeded when Hindsight answered
502.
"""

import hashlib
import logging
import time
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from memory import ids, metrics
from memory.config import get_settings
from memory.db import session_scope
from memory.models import ActivityEvent

logger = logging.getLogger("memory.activity")

_call: ContextVar[dict | None] = ContextVar("memory_activity_call", default=None)

# Module-level, so pruning costs one DELETE an hour per process instead of
# one per call. N replicas run the same idempotent DELETE N times, which is
# cheaper than any coordination would be.
_PRUNE_INTERVAL_SECONDS = 3600.0
# -inf, not 0.0: `time.monotonic()` is time since BOOT on Linux, so with a 0.0
# start the first prune is skipped on any host whose uptime is under the
# interval -- the whole first hour after a reboot, and every fresh CI runner
# (measured: CI failed test_prune_drops_rows_past_the_horizon while a dev box
# with days of uptime passed it). -inf makes the first call always prune,
# independent of how long the machine has been up.
_last_prune = float("-inf")

_AGENT_MAX = 64


def fingerprint(bank_id: str) -> str:
    """What stands in for the bank id everywhere it would otherwise appear.

    SPEC inv. 29: the bank id may not leave this service. This is stable
    across restarts (so it correlates with Hindsight's own logs) and not
    reversible.
    """
    return hashlib.sha256(bank_id.encode()).hexdigest()[:12]


def new_call() -> None:
    _call.set({"t0": time.monotonic()})


def describe(**fields: object) -> None:
    """Fill in what this call turned out to be. No-op outside a call."""
    call = _call.get()
    if call is not None:
        call.update(fields)


def set_error(code: str) -> None:
    describe(error_code=code)


def finish(surface: str) -> None:
    """Write the row and the metrics. Never raises."""
    call = _call.get()
    _call.set(None)
    # No action means the call never reached _resolve_bank: a rejected
    # credential, a /metrics scrape, a 404. There is no bank to report -- and
    # on a public ingress, recording anonymous traffic would be an unbounded
    # INSERT vector. Rejections are still visible as
    # memory_errors_total{code="UNAUTHORIZED"}. Missing scope is treated the
    # same way: it is only ever absent alongside a missing action, but
    # checking it here (rather than indexing call["scope"] below) is what
    # keeps this guard -- not the try/except -- responsible for guaranteeing
    # finish() never raises.
    if call is None or "action" not in call or "scope" not in call:
        return

    duration = time.monotonic() - call["t0"]
    outcome = "error" if call.get("error_code") else "ok"
    action = call["action"]
    scope = call["scope"]

    try:
        metrics.CALLS.labels(
            action=action, scope=scope, surface=surface, outcome=outcome
        ).inc()
        metrics.CALL_DURATION.labels(action=action, surface=surface).observe(duration)
        if call.get("content_bytes"):
            metrics.CONTENT_BYTES.labels(scope=scope).inc(call["content_bytes"])

        agent = call.get("agent")
        with session_scope() as db:
            db.add(
                ActivityEvent(
                    id=ids.new_activity_id(),
                    tenant_id=call["tenant_id"],
                    credential_id=call.get("credential_id"),
                    action=action,
                    surface=surface,
                    scope=scope,
                    user_id=call.get("user_id"),
                    project_slug=call.get("project_slug"),
                    bank_fingerprint=call["bank_fingerprint"],
                    document_id=call.get("document_id"),
                    content_bytes=call.get("content_bytes"),
                    # Truncated, not rejected: `agent` is client-supplied and
                    # the column is String(64). An over-long value would be a
                    # psycopg DataError raised from telemetry -- which must
                    # never be what turns a served request into a 500.
                    agent=str(agent)[:_AGENT_MAX] if agent else None,
                    outcome=outcome,
                    error_code=call.get("error_code"),
                    duration_ms=int(duration * 1000),
                )
            )
            db.commit()
            # Its own try/except: the row above is already committed, so a
            # prune failure here must not be logged as "activity not
            # recorded" -- that would misreport a row that in fact landed.
            try:
                _prune(db)
            except Exception as exc:  # noqa: BLE001 -- must never propagate
                logger.warning("activity prune failed: %s", type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 -- must never propagate, see docstring
        # Telemetry is never worth an error the caller can see. Type only --
        # the exception text can carry SQL or a bank id, which is why
        # db.py sets hide_parameters=True.
        logger.warning("activity not recorded: %s", type(exc).__name__)


def _prune(db: Session) -> None:
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < _PRUNE_INTERVAL_SECONDS:
        return
    _last_prune = now

    days = get_settings().activity_retention_days
    if days <= 0:  # 0 means keep everything
        return
    cutoff = datetime.now(UTC) - timedelta(days=days)
    db.execute(delete(ActivityEvent).where(ActivityEvent.created_at < cutoff))
    db.commit()
