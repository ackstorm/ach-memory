"""SPEC §7.5: `POST /v1/bootstrap` -- an idempotent pre-warm, kept
structurally separate from ordinary reads (`/v1/mental-models`,
`/v1/context/load`), which never reconcile a definition.

It survives the removal of the internal identity system because it is the
only thing that provisions a caller's own bank before their first prompt:
`link_identity` deliberately does not (a Hindsight round trip on the
authentication path), and no read path does either. It creates nothing --
see `memory.bootstrap` for why that is now `retain`'s job alone.

Authenticated as the caller, never for somebody else: it bootstraps whoever
holds the token and has no target parameter, so it never needed operator
authority and does not have one now.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from memory.api.app import current_principal
from memory.auth.principal import Principal
from memory.bootstrap import BootstrapRequest, BootstrapResult, bootstrap
from memory.db import get_session
from memory.hindsight.client import get_client

router = APIRouter(prefix="/v1/bootstrap", tags=["bootstrap"])


@router.post("", response_model=BootstrapResult)
def bootstrap_route(
    body: BootstrapRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> BootstrapResult:
    return bootstrap(db, principal, body, client=get_client())
