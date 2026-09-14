"""Provision a bank and its built-in mental models before the first retain."""

from sqlalchemy.orm import Session

from memory.backend.base import Backend
from memory.bank_ref import LogicalBankRef
from memory.builtin_models import BUILTIN_MODELS
from memory.errors import UnsupportedCapability


def provision_before_retain(db: Session, ref: LogicalBankRef, *, backend: Backend) -> None:
    """Ensure `ref.bank_id` exists and carries its scope's built-in models.
    Both adapter calls are idempotent, so this needs no bookkeeping of its
    own and is safe to call on every retain. An adapter without the
    `mental_models` capability still works; it just serves no built-ins."""
    backend.provision(ref.bank_id)
    builtins = [m for m in BUILTIN_MODELS if m.scope == ref.scope]
    try:
        backend.provision_mental_models(ref.bank_id, builtins)
    except UnsupportedCapability:
        pass
