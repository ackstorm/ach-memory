"""SPEC §7.5: `POST /v1/bootstrap` -- idempotent MCP/user/project
provisioning, kept structurally separate from ordinary reads (`/v1/mental-
models`, `/v1/context/load`), which never create or reconcile a definition.

A project or user created through the control plane (`POST /v1/projects`,
`POST /v1/users`) is provisioned the same way at creation time, so this
route is a pre-warm rather than a prerequisite -- it is what makes an MCP
session's first prompt warm rather than cold, and repairs anything a failed
creation left half-done.
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
