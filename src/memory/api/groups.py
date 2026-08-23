from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory import audit, ids
from memory.api.app import current_on_behalf_of, require_master
from memory.api.identifiers import reject_control_characters
from memory.auth.principal import Principal
from memory.db import ensure_tenant, get_session
from memory.errors import GroupAlreadyExists, GroupNotFound, UserNotFound
from memory.models import Group, GroupMember, User

router = APIRouter(prefix="/v1/groups", tags=["groups"])


class CreateGroupRequest(BaseModel):
    # Bounded to match Group.id/Group.name (String(128)/String(256)) so an
    # oversize value is a typed 422 at the boundary, not a 500 (DataError, not
    # IntegrityError -- db.begin_nested()'s except clause never sees a length
    # overflow) from the DB. Same pattern as CreateUserRequest.id.
    id: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=256)


class GroupResponse(BaseModel):
    group_id: str
    name: str | None
    members: list[str]


def _load(db: Session, principal: Principal, group_id: str) -> Group:
    reject_control_characters(group_id, GroupNotFound)
    group = db.get(Group, group_id)
    if group is None or group.tenant_id != principal.tenant_id:
        raise GroupNotFound(group_id=group_id)
    return group


def _members(db: Session, group_id: str) -> list[str]:
    return sorted(
        db.scalars(
            select(GroupMember.user_id).where(GroupMember.group_id == group_id)
        ).all()
    )


@router.post("", status_code=201, response_model=GroupResponse)
def create_group(
    body: CreateGroupRequest,
    principal: Annotated[Principal, Depends(require_master)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> GroupResponse:
    ensure_tenant(db, principal.tenant_id)
    reject_control_characters(body.id, GroupAlreadyExists)

    group = Group(
        id=body.id or ids.new_group_id(),
        tenant_id=principal.tenant_id,
        name=body.name,
    )
    try:
        with db.begin_nested():
            db.add(group)
    except IntegrityError as exc:
        # Reusing an explicit id is the documented platform-provisioning path,
        # so a duplicate is an ordinary client mistake — 409, not a 500 from
        # the catch-all handler. Same shape as create_user.
        raise GroupAlreadyExists("a group with that id already exists") from exc
    audit.record(db, principal, "group.create", group.id, on_behalf_of=on_behalf_of)
    db.commit()
    return GroupResponse(group_id=group.id, name=group.name, members=[])


@router.get("", response_model=list[GroupResponse])
def list_groups(
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> list[GroupResponse]:
    groups = db.scalars(
        select(Group).where(Group.tenant_id == principal.tenant_id).order_by(Group.id)
    ).all()
    return [
        GroupResponse(group_id=g.id, name=g.name, members=_members(db, g.id))
        for g in groups
    ]


@router.get("/{group_id}", response_model=GroupResponse)
def get_group(
    group_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> GroupResponse:
    group = _load(db, principal, group_id)
    return GroupResponse(
        group_id=group.id, name=group.name, members=_members(db, group.id)
    )


@router.put("/{group_id}/members/{user_id}", status_code=204)
def add_member(
    group_id: str,
    user_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> Response:
    _load(db, principal, group_id)
    reject_control_characters(user_id, UserNotFound)
    user = db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        # The group exists; the user does not. Saying GROUP_NOT_FOUND here
        # would send the caller looking in the wrong place.
        raise UserNotFound(user_id=user_id)

    # PUT is idempotent: adding an existing member is success, not a conflict.
    if db.get(GroupMember, (group_id, user_id)) is None:
        db.add(GroupMember(group_id=group_id, user_id=user_id))
        audit.record(
            db,
            principal,
            "group.add_member",
            f"{group_id}/{user_id}",
            on_behalf_of=on_behalf_of,
        )
        db.commit()
    return Response(status_code=204)


@router.delete("/{group_id}/members/{user_id}", status_code=204)
def remove_member(
    group_id: str,
    user_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> Response:
    _load(db, principal, group_id)
    reject_control_characters(user_id, UserNotFound)
    membership = db.get(GroupMember, (group_id, user_id))
    if membership is not None:
        db.delete(membership)
        audit.record(
            db,
            principal,
            "group.remove_member",
            f"{group_id}/{user_id}",
            on_behalf_of=on_behalf_of,
        )
        db.commit()
    return Response(status_code=204)
