from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from memory.api.app import current_principal
from memory.auth.principal import Principal
from memory.context_service import load_context
from memory.db import get_session
from memory.v040_contracts import LoadContextRequest

router = APIRouter(prefix="/v1/context", tags=["context"])


@router.post("/load")
def context_load(body: LoadContextRequest, principal: Annotated[Principal, Depends(current_principal)], db: Session = Depends(get_session)):
    return load_context(db, principal, body)
