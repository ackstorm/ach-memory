"""POST /v1/read/recall, POST /v1/read/history -- the read-only recall
surface (Phase 5 plan). Every route here resolves through
`read_context.resolve_read_bank` (always `create=False`, no `git_locator`)
and returns only `read_models.py`'s closed, whitelisted response shapes --
never Hindsight's own raw payload.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from memory import read_service
from memory.api.app import current_on_behalf_of, current_principal
from memory.auth.principal import Principal
from memory.db import get_session
from memory.read_models import HistoryRequest, HistoryResponse, RecallRequest, RecallResponse

router = APIRouter(prefix="/v1/read", tags=["read"])


@router.post("/recall", response_model=RecallResponse)
def recall(
    body: RecallRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> RecallResponse:
    return read_service.recall(db, principal, on_behalf_of, body)


@router.post("/history", response_model=HistoryResponse)
def history(
    body: HistoryRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> HistoryResponse:
    return read_service.history(db, principal, on_behalf_of, body)
