"""Durable idempotency and provenance primitives for typed retain."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.errors import IdempotencyConflict
from memory.models import Project, RetainedRecord, User
from memory.v040_contracts import TypedRetainRequest


@dataclass(frozen=True)
class LogicalBankRef:
    tenant_id: str
    scope: Literal["user", "project"]
    user_id: str | None
    project_internal_id: str | None
    bank_id: str = field(repr=False)

    def __post_init__(self) -> None:
        valid_user = (
            self.scope == "user" and self.user_id is not None and self.project_internal_id is None
        )
        valid_project = (
            self.scope == "project"
            and self.user_id is None
            and self.project_internal_id is not None
        )
        if not (valid_user or valid_project):
            raise ValueError("a logical bank needs exactly one matching scope identity")
        if not self.tenant_id or not self.bank_id:
            raise ValueError("a logical bank needs tenant and physical bank identity")


def _bank_filters(model, bank: LogicalBankRef) -> tuple:
    return (
        model.tenant_id == bank.tenant_id,
        model.scope == bank.scope,
        model.user_id == bank.user_id,
        model.project_internal_id == bank.project_internal_id,
    )


def _lock_bank(db: Session, bank: LogicalBankRef) -> None:
    """Serialize control-plane writes even before a child row exists."""
    if bank.scope == "user":
        query = select(User.id).where(
            User.tenant_id == bank.tenant_id,
            User.id == bank.user_id,
            User.bank_id == bank.bank_id,
        )
    else:
        query = select(Project.internal_id).where(
            Project.tenant_id == bank.tenant_id,
            Project.internal_id == bank.project_internal_id,
            Project.bank_id == bank.bank_id,
        )
    if db.execute(query.with_for_update()).scalar_one_or_none() is None:
        raise ValueError("logical bank does not match a persisted physical bank")


def _operation_query(
    bank: LogicalBankRef, operation_id: str | UUID
) -> Select[tuple[RetainedRecord]]:
    return select(RetainedRecord).where(
        *_bank_filters(RetainedRecord, bank),
        RetainedRecord.operation_id == str(operation_id),
    )


def _canonical_expiry(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _payload_hash(
    bank: LogicalBankRef,
    request: TypedRetainRequest,
    canonical_content: str,
    sanitized_evidence: list[dict[str, str | None]],
) -> str:
    payload = {
        "schema": "ach-retain-idempotency-v1",
        "scope": {
            "tenant_id": bank.tenant_id,
            "scope": bank.scope,
            "user_id": bank.user_id,
            "project_internal_id": bank.project_internal_id,
        },
        "content": canonical_content,
        "memory_type": request.memory_type,
        "basis": request.basis,
        "trigger": request.trigger,
        "valid_until": _canonical_expiry(request.valid_until),
        "sanitized_evidence": sanitized_evidence,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _document_id(operation_id: UUID) -> str:
    return f"ach-retain-{operation_id.hex}"


def accept_retain(
    db: Session,
    principal: Principal,
    request: TypedRetainRequest,
    *,
    bank: LogicalBankRef,
    canonical_content: str,
    sanitized_evidence: list[dict[str, str | None]],
) -> tuple[RetainedRecord, bool]:
    if principal.tenant_id != bank.tenant_id or request.scope != bank.scope:
        raise ValueError("principal, request and logical bank scopes must match")
    if bank.scope == "user" and not principal.is_master and principal.user_id != bank.user_id:
        raise ValueError("a user principal cannot persist another user's retain")

    digest = _payload_hash(bank, request, canonical_content, sanitized_evidence)
    _lock_bank(db, bank)
    existing = db.scalar(_operation_query(bank, request.operation_id).with_for_update())
    if existing is not None:
        if existing.payload_hash != digest:
            raise IdempotencyConflict(
                "operation_id was already used with a different canonical payload",
                operation_id=str(request.operation_id),
            )
        return existing, False

    row = RetainedRecord(
        tenant_id=bank.tenant_id,
        scope=bank.scope,
        user_id=bank.user_id,
        project_internal_id=bank.project_internal_id,
        operation_id=str(request.operation_id),
        payload_hash=digest,
        document_id=_document_id(request.operation_id),
        canonical_content=canonical_content,
        memory_type=request.memory_type,
        basis=request.basis,
        trigger=request.trigger,
        sanitized_evidence=sanitized_evidence,
        valid_until=request.valid_until,
        lifecycle="active",
        upstream_state="pending",
        calling_agent=None,
        created_by_credential=principal.credential_id or principal.key_id,
    )
    db.add(row)
    db.flush()
    return row, True


def get_by_operation(
    db: Session, bank: LogicalBankRef, operation_id: str | UUID
) -> RetainedRecord | None:
    return db.scalar(_operation_query(bank, operation_id))
