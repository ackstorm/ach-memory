"""Authorized, bounded standing-context assembly."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from memory import mental_model_service, projects, working_state
from memory.auth.principal import Principal
from memory.currentness import bank_is_withheld
from memory.delivery import (
    ContextPayload,
    DeliveryOmission,
    DeliverySection,
    assemble_context,
    count_tokens,
)
from memory.errors import DomainError, ProjectNotFound
from memory.hindsight.client import get_client
from memory.models import MentalModelRegistration, Project, RetainedRecord
from memory.read_context import resolve_read_bank
from memory.retained_records import LogicalBankRef
from memory.v040_contracts import LoadContextRequest

DEADLINE_SECONDS = 2.0
ACTIVE_CLAIMS_TOKENS = 256
# An ordered, bounded prefix -- never the whole ledger. Generously larger
# than the 256-token budget could ever hold (even single-word claims run a
# few tokens apiece once the "Scope · " prefix and separators are counted),
# so the fetch bound never itself becomes the reason a claim that would have
# fit is left out.
ACTIVE_CLAIMS_FETCH_LIMIT = 256


def _model_text(raw: dict) -> str:
    for key in ("text", "content", "response", "output"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return ""


def _bounded_active_claims(
    entries: list[tuple[str, RetainedRecord]], *, total_available: int
) -> tuple[str, int]:
    """`entries` is the already-fetched, ordered, bounded prefix (soonest
    expiry first); `total_available` is the TRUE count across all scopes,
    which may exceed `len(entries)` when the ledger is larger than the fetch
    bound. The reported omission always reflects that true gap -- rows never
    even fetched are omitted just as surely as ones trimmed for the token
    budget."""
    lines = [f"{scope.title()} · {record.canonical_content}" for scope, record in entries]
    never_fetched = max(0, total_available - len(lines))
    for kept in range(len(lines), -1, -1):
        omitted = never_fetched + (len(lines) - kept)
        marker = (
            [f"[{omitted} more active claims omitted; use recall]"]
            if omitted
            else []
        )
        body = "\n".join([*lines[:kept], *marker])
        if count_tokens(body) <= ACTIVE_CLAIMS_TOKENS:
            return body, omitted
    return "", total_available


class ContextService:
    def __init__(
        self,
        db: Session,
        principal: Principal,
        *,
        client=None,
        clock: Callable[[], float] = monotonic,
    ):
        self.db = db
        self.principal = principal
        self.client = client or get_client()
        self.clock = clock

    def _bank(self, scope: str, project: Project | None = None) -> LogicalBankRef:
        if scope == "user":
            bank = resolve_read_bank(self.db, self.principal, None, "context.load", "user")
            return LogicalBankRef(self.principal.tenant_id, "user", self.principal.user_id, None, bank.bank_id)
        assert project is not None
        return LogicalBankRef(self.principal.tenant_id, "project", None, project.internal_id, project.bank_id)

    def _observed_ready(self, row, bank: LogicalBankRef, remaining) -> bool:
        """Reconcile one withheld model with its own recorded operation.

        A finished upstream synthesis becomes deliverable only when
        something observes it, and until now nothing on this path ever did:
        `get_mental_model` was the sole caller of `observe_model_refresh`,
        so a bank whose built-ins were still withheld delivered empty
        standing context for ever. That is precisely what the SessionStart
        hook does -- `ach-memory context load` and nothing else -- so on a
        fresh bank the hook could never deliver the very models a first
        retain had just registered for it. Measured 2026-09-07: both built-ins sat
        `withheld`/`pending` for 40 minutes after their refresh operations
        had already completed upstream, and one `get_mental_model` call
        flipped each to `ready` immediately.

        Bounded and fail-open like every other phase here: skipped with no
        time left, and an unavailable backend leaves the model withheld for
        a later access rather than costing the caller its whole context.
        """
        if remaining() <= 0:
            return False
        try:
            observed = mental_model_service.observe_model_refresh(
                self.db, bank, row.model_key, client=self.client
            )
        except DomainError:
            return False
        return observed.delivery_state == "ready"

    def _set_statement_timeout(self, remaining_seconds: float) -> None:
        """A transaction-local backstop for a query that turns out to be
        slower than expected -- `0` means "no timeout" to PostgreSQL, the
        opposite of what a caller with no time left wants, so this is only
        ever called when `remaining_seconds > 0` (callers check first).

        `SET LOCAL` does not accept a bind parameter (PostgreSQL requires a
        literal here) -- safe to inline since `ms` is always our own
        `int(...)` computation, never caller-supplied text.
        """
        ms = max(1, int(remaining_seconds * 1000))
        self.db.execute(text(f"SET LOCAL statement_timeout = {ms}"))

    def load(self, request: LoadContextRequest) -> ContextPayload:
        # Computed ONCE, here -- every phase below measures against this
        # same deadline, and none of them ever recomputes it.
        deadline = self.clock() + DEADLINE_SECONDS

        def remaining() -> float:
            return max(0.0, deadline - self.clock())

        project = None
        # None when no project_slug was given at all -- nothing to report.
        # "absent" covers both a missing project and a forbidden one (SPEC
        # decision 4): the agent stays blind either way (the delivered
        # content is identically empty), but this field lets a facade log
        # the distinction without ever exposing which of the two it was --
        # ProjectAccessDenied's owner_type never reaches here.
        project_status: str | None = None
        if request.project_slug:
            try:
                project = projects.resolve(
                    self.db, self.principal, request.project_slug, create=False
                ).project
                project_status = "ready"
            except ProjectNotFound:
                # load_context is one of the twelve read tools that map an
                # absent project to empty rather than an error (lazy-
                # provisioning plan, decision 3): an agent does not know
                # whether today is its first day, and its first call is this
                # one, never retain. Treated exactly like no project_slug
                # having been given at all -- the user half still delivers.
                project = None
                project_status = "absent"
        user_bank = self._bank("user")
        project_bank = self._bank("project", project) if project else None
        sections: list[DeliverySection] = []
        omissions: list[DeliveryOmission] = []
        if project_bank is None and request.scope != "user":
            # Say so rather than just returning the user half. A caller that
            # asked bare cannot otherwise tell "this workspace has no project"
            # from "the project section was dropped" -- and an empty
            # `omissions` actively asserts nothing is missing. Reached both
            # when the request carried no project_slug and when it named one
            # that does not exist (or is not this caller's) -- both
            # indistinguishable here on purpose. Suppressed under
            # scope="user": that caller deliberately excluded the project
            # half, so its absence is not a gap to report.
            omissions.append(
                DeliveryOmission(key="project", reason="no_project_resolved")
            )
        banks = []
        if request.scope in ("user", "both"):
            banks.append(("user", user_bank))
        if project_bank is not None and request.scope in ("project", "both"):
            banks.append(("project", project_bank))
        bank_by_scope = dict(banks)
        withheld_scopes = {
            scope for scope, bank in banks if bank_is_withheld(self.db, bank)
        }
        omissions.extend(
            DeliveryOmission(
                key=f"{scope}-bank", reason="bank_currentness_unavailable"
            )
            for scope in sorted(withheld_scopes)
        )
        registration_scope = (
            ((MentalModelRegistration.scope == "user") & (MentalModelRegistration.user_id == self.principal.user_id))
            | ((MentalModelRegistration.scope == "project") & (MentalModelRegistration.project_internal_id == (project.internal_id if project else None)))
        )
        jobs = []
        if remaining() > 0:
            self._set_statement_timeout(remaining())
            # Standing context is built-ins and nothing else: being a built-in
            # IS the delivery decision, so there is no flag to read. `active`
            # rather than `!= "deleted"` because an operator-disabled built-in
            # must stay out, and disabling is now the only way to opt one out.
            rows = list(self.db.scalars(select(MentalModelRegistration).where(
                MentalModelRegistration.tenant_id == self.principal.tenant_id,
                registration_scope,
                MentalModelRegistration.origin == "builtin",
                MentalModelRegistration.lifecycle_state == "active",
            )))
            for row in rows:
                bank = bank_by_scope.get(row.scope)
                if (
                    bank is None
                    or row.scope in withheld_scopes
                    or not row.upstream_model_id
                ):
                    continue
                if row.delivery_state != "ready" and not self._observed_ready(
                    row, bank, remaining
                ):
                    continue
                jobs.append((row, bank))
        else:
            # The deadline was already gone before the registry could even
            # be queried (e.g. slow project/bank resolution) -- which
            # specific always-in-context models would have been considered
            # is unknown, but the omission must still be machine-readable
            # rather than the section just silently never appearing.
            omissions.append(
                DeliveryOmission(key="always-in-context-models", reason="deadline_exceeded")
            )
        if jobs and remaining() > 0:
            pool = ThreadPoolExecutor(max_workers=min(len(jobs), 9))

            def fetch(row, bank):
                call_timeout = max(0.001, remaining())
                return self.client.get_mental_model(
                    bank.bank_id, row.upstream_model_id, timeout=call_timeout
                )

            futures = {
                pool.submit(fetch, row, bank): (row, bank) for row, bank in jobs
            }
            # Real wall-clock wait, bounded by whatever `remaining()` reports
            # right now -- never re-derived from `deadline` a second time.
            done, pending = wait(futures, timeout=remaining())
            for future in pending:
                row, _ = futures[future]
                future.cancel()
                omissions.append(
                    DeliveryOmission(key=row.model_key, reason="model_unavailable")
                )
            for future in done:
                row, _ = futures[future]
                try:
                    raw = future.result()
                    model_text = _model_text(raw if isinstance(raw, dict) else {})
                    if model_text:
                        prefix = "0" if row.scope == "user" else "2"
                        sections.append(
                            DeliverySection(
                                f"{prefix}:{row.model_key}",
                                f"{row.scope.title()} · {row.model_key}",
                                model_text,
                                row.max_tokens,
                            )
                        )
                except Exception:  # noqa: BLE001 - one unavailable model cannot cancel peers
                    omissions.append(
                        DeliveryOmission(key=row.model_key, reason="model_unavailable")
                    )
            # An over-deadline peer must never delay the response: cancel
            # whatever is still outstanding instead of waiting for threads
            # blocked on a client call past the timeout it was given.
            pool.shutdown(wait=False, cancel_futures=True)
        elif jobs:
            # The deadline was already exhausted before this phase even
            # started (e.g. project resolution or the registry query itself
            # ran long) -- every candidate model is unavailable, none tried.
            for row, _ in jobs:
                omissions.append(
                    DeliveryOmission(key=row.model_key, reason="model_unavailable")
                )
        if project is not None and request.scope != "user":
            metadata = "\n".join(filter(None, [f"name: {project.name}" if project.name else None, f"purpose: {project.purpose}" if project.purpose else None, f"spec: {project.canonical_spec}" if project.canonical_spec else None]))
            if metadata:
                sections.append(DeliverySection("1:project-metadata", "Project Metadata", metadata, 256))
        now = datetime.now(UTC)
        active_claims: list[tuple[str, RetainedRecord]] = []
        total_available = 0
        claims_deadline_exceeded = False
        for scope, bank in banks:
            if scope in withheld_scopes:
                continue
            if remaining() <= 0:
                # Either the whole budget was already gone before this phase
                # started, or an earlier scope's own queries used the rest --
                # stop before a scope's query ever runs on a near-zero
                # budget. `_set_statement_timeout` clamps to a 1ms floor, and
                # a real query PostgreSQL cancels under that floor raises an
                # uncaught OperationalError instead of a graceful omission.
                claims_deadline_exceeded = True
                break
            filters = (
                RetainedRecord.tenant_id == self.principal.tenant_id,
                RetainedRecord.scope == scope,
                RetainedRecord.user_id == (bank.user_id if scope == "user" else None),
                RetainedRecord.project_internal_id == (bank.project_internal_id if scope == "project" else None),
                RetainedRecord.lifecycle == "active",
                RetainedRecord.upstream_state.in_(("accepted", "completed")),
                RetainedRecord.valid_until.is_not(None),
                RetainedRecord.valid_until > now,
            )
            self._set_statement_timeout(remaining())
            total_available += (
                self.db.scalar(select(func.count()).select_from(RetainedRecord).where(*filters)) or 0
            )
            # An ordered, bounded prefix -- never the whole ledger (SPEC's
            # currentness-over-availability boundary applies to database
            # work too, not just the Hindsight read phase).
            claims = self.db.scalars(
                select(RetainedRecord)
                .where(*filters)
                .order_by(RetainedRecord.valid_until, RetainedRecord.recorded_at, RetainedRecord.document_id)
                .limit(ACTIVE_CLAIMS_FETCH_LIMIT)
            ).all()
            active_claims.extend((scope, record) for record in claims)
        active_claims.sort(
            key=lambda item: (
                item[1].valid_until,
                item[1].recorded_at,
                item[1].document_id,
            )
        )
        active_claims = active_claims[:ACTIVE_CLAIMS_FETCH_LIMIT]
        if claims_deadline_exceeded:
            omissions.append(DeliveryOmission(key="active-claims", reason="deadline_exceeded"))
        claims_text, claims_omitted = _bounded_active_claims(
            active_claims, total_available=total_available
        )
        if claims_text:
            sections.append(
                DeliverySection(
                    "3:active-claims",
                    "Active Time-Bounded Claims",
                    claims_text,
                    ACTIVE_CLAIMS_TOKENS,
                )
            )
        if claims_omitted:
            omissions.append(
                DeliveryOmission(
                    key="active-claims",
                    reason="section_budget",
                    omitted_count=claims_omitted,
                )
            )
        if project is not None and request.workspace_id:
            if remaining() > 0:
                self._set_statement_timeout(remaining())
                state = working_state.get_current(self.db, self.principal, project.internal_id, request.workspace_id)
                if state:
                    rendered = working_state.render_full_section(state, datetime.now(UTC)).text
                    sections.append(DeliverySection("4:working-state", "Working State", rendered, 512))
            else:
                omissions.append(DeliveryOmission(key="working-state", reason="deadline_exceeded"))
        payload = assemble_context(sections)
        payload.omissions.extend(omissions)
        payload.project_status = project_status
        return payload


def load_context(db: Session, principal: Principal, request: LoadContextRequest) -> ContextPayload:
    """The route-facing entry point. Tests that need a stubbed Hindsight or a
    stubbed clock construct `ContextService` directly; nothing in production
    overrides either, so this takes no injection parameters."""
    return ContextService(db, principal).load(request)
