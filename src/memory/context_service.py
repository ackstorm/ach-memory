"""Authorized, bounded standing-context assembly."""

from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory import projects, working_state
from memory.auth.principal import Principal
from memory.currentness import bank_is_withheld
from memory.delivery import DeliverySection, ContextPayload, assemble_context
from memory.hindsight.client import get_client
from memory.models import MentalModelRegistration, Project, RetainedRecord
from memory.read_context import resolve_read_bank
from memory.retained_records import LogicalBankRef
from memory.v040_contracts import LoadContextRequest


DEADLINE_SECONDS = 2.0


def _model_text(raw: dict) -> str:
    for key in ("text", "content", "response", "output"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return ""


class ContextService:
    def __init__(self, db: Session, principal: Principal, *, client=None):
        self.db = db
        self.principal = principal
        self.client = client or get_client()

    def _bank(self, scope: str, project: Project | None = None) -> LogicalBankRef:
        if scope == "user":
            bank = resolve_read_bank(self.db, self.principal, None, "context.load", "user")
            return LogicalBankRef(self.principal.tenant_id, "user", self.principal.user_id, None, bank.bank_id)
        assert project is not None
        return LogicalBankRef(self.principal.tenant_id, "project", None, project.internal_id, project.bank_id)

    def load(self, request: LoadContextRequest) -> ContextPayload:
        started = monotonic()
        project = None
        if request.project_slug:
            project = projects.resolve(self.db, self.principal, request.project_slug, create=False).project
        user_bank = self._bank("user")
        project_bank = self._bank("project", project) if project else None
        sections: list[DeliverySection] = []
        omissions = []
        rows = list(self.db.scalars(select(MentalModelRegistration).where(
            MentalModelRegistration.tenant_id == self.principal.tenant_id,
            MentalModelRegistration.lifecycle_state != "deleted",
            MentalModelRegistration.always_in_context.is_(True),
        )))
        jobs = []
        for row in rows:
            bank = user_bank if row.scope == "user" else project_bank
            if bank is None or bank_is_withheld(self.db, bank) or row.delivery_state != "ready" or not row.upstream_model_id:
                continue
            jobs.append((row, bank))
        remaining = max(0.0, DEADLINE_SECONDS - (monotonic() - started))
        with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
            futures = {pool.submit(self.client.get_mental_model, bank.bank_id, row.upstream_model_id): (row, bank) for row, bank in jobs}
            try:
                completed = as_completed(futures, timeout=remaining or 0.001)
                iterator = completed
                for future in iterator:
                    row, bank = futures[future]
                    try:
                        raw = future.result()
                        text = _model_text(raw if isinstance(raw, dict) else {})
                        if text:
                            prefix = "0" if row.scope == "user" else "2"
                            sections.append(DeliverySection(f"{prefix}:{row.model_key}", f"{row.scope.title()} · {row.model_key}", text, row.max_tokens))
                    except Exception:
                        omissions.append({"key": row.model_key, "reason": "model_unavailable"})
            except TimeoutError:
                for future, (row, _) in futures.items():
                    if not future.done():
                        future.cancel()
                        omissions.append({"key": row.model_key, "reason": "model_unavailable"})
        if project is not None:
            metadata = "\n".join(filter(None, [f"name: {project.name}" if project.name else None, f"purpose: {project.purpose}" if project.purpose else None, f"spec: {project.canonical_spec}" if project.canonical_spec else None]))
            if metadata:
                sections.append(DeliverySection("1:project-metadata", "Project Metadata", metadata, 256))
            now = datetime.now(UTC)
            for scope, bank in (("user", user_bank), ("project", project_bank)):
                if bank is None or bank_is_withheld(self.db, bank):
                    continue
                claims = self.db.scalars(select(RetainedRecord).where(
                    RetainedRecord.tenant_id == self.principal.tenant_id,
                    RetainedRecord.scope == scope,
                    RetainedRecord.lifecycle == "active",
                    RetainedRecord.valid_until.is_not(None),
                    RetainedRecord.valid_until > now,
                ).order_by(RetainedRecord.valid_until, RetainedRecord.recorded_at, RetainedRecord.document_id)).all()
                text = "\n".join(record.canonical_content for record in claims)
                if text:
                    sections.append(DeliverySection("2:active-claims", "Active Time-Bounded Claims", text, 256))
        if project is not None and request.workspace_id:
            state = working_state.get_current(self.db, self.principal, project.internal_id, request.workspace_id)
            if state:
                rendered = working_state.render_full_section(state, datetime.now(UTC)).text
                sections.append(DeliverySection("3:working-state", "Working State", rendered, 512))
        payload = assemble_context(sections)
        payload.omissions.extend(omissions)
        return payload


def load_context(db: Session, principal: Principal, request: LoadContextRequest, *, client=None) -> ContextPayload:
    return ContextService(db, principal, client=client).load(request)
