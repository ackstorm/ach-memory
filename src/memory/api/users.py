from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory import audit, ids
from memory.api.app import current_on_behalf_of, require_master
from memory.auth import keys
from memory.auth.principal import Principal
from memory.db import ensure_tenant, get_session
from memory.errors import KeyNotFound, UserAlreadyExists, UserNotFound
from memory.identifiers import reject_control_characters
from memory.models import ApiKey, User

router = APIRouter(prefix="/v1/users", tags=["users"])


class CreateUserRequest(BaseModel):
    # extra="forbid": `{"user_id": "ach-user-82f"}` (SPEC §16.3's field is
    # `id`) used to 201 with a service-generated id while ACH's own id was
    # silently dropped -- a provisioning failure indistinguishable from
    # success.
    model_config = ConfigDict(extra="forbid")

    # Bounded to match User.id (String(128)) so an oversize explicit id is a
    # typed 422 at the boundary, not a 500 (DataError, not IntegrityError --
    # db.begin_nested()'s except clause never sees a length overflow) from
    # the DB. Same reasoning as git_locator's bound in memory/api/memory.py.
    id: str | None = Field(default=None, max_length=128)

    @field_validator("id")
    @classmethod
    def _no_control_characters(cls, value: str | None) -> str | None:
        # Same bound as oversize, same shape: a control character is also an
        # unstorable id, so it gets the same 422 rather than a 409 that reads
        # as USER_ALREADY_EXISTS for an id that never existed. Mirrors
        # ScopedRequest.user_id's validator in memory/api/memory.py.
        if value and any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
            raise ValueError("id must not contain control characters")
        return value


class CreateUserResponse(BaseModel):
    user_id: str
    created_at: str


class CreateKeyResponse(BaseModel):
    key_id: str
    key: str


class UserSummary(BaseModel):
    user_id: str
    created_at: str


class ListUsersResponse(BaseModel):
    users: list[UserSummary]


class KeySummary(BaseModel):
    # No secret, no secret_hash: the plaintext exists once, in the mint
    # response (SPEC §5.3), and the hash is what protects it.
    key_id: str
    status: str
    created_at: str


class ListKeysResponse(BaseModel):
    keys: list[KeySummary]


@router.post("", status_code=201, response_model=CreateUserResponse)
def create_user(
    body: CreateUserRequest,
    principal: Annotated[Principal, Depends(require_master)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> CreateUserResponse:
    """Provisioning. An explicit id is the ACH path; omitting it is standalone."""
    ensure_tenant(db, principal.tenant_id)

    user = User(
        id=body.id or ids.new_user_id(),
        tenant_id=principal.tenant_id,
        bank_id=ids.new_user_bank_id(),
    )
    try:
        with db.begin_nested():
            db.add(user)
    except IntegrityError as exc:
        # A caller-supplied id that already exists is an ordinary client
        # mistake, not a server fault. A savepoint, not a bare rollback: this
        # handler is small today, but the same shape guards the project-creation
        # race (SPEC §9) where earlier writes must survive the conflict.
        raise UserAlreadyExists("a user with that id already exists") from exc
    audit.record(db, principal, "user.create", user.id, on_behalf_of=on_behalf_of)
    db.commit()
    return CreateUserResponse(user_id=user.id, created_at=user.created_at.isoformat())


@router.get("/{user_id}", response_model=CreateUserResponse)
def get_user(
    user_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> CreateUserResponse:
    reject_control_characters(user_id, UserNotFound)
    user = db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        raise UserNotFound(user_id=user_id)
    return CreateUserResponse(user_id=user.id, created_at=user.created_at.isoformat())


@router.post("/{user_id}/keys", status_code=201, response_model=CreateKeyResponse)
def create_key(
    user_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> CreateKeyResponse:
    reject_control_characters(user_id, UserNotFound)
    user = db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        raise UserNotFound(user_id=user_id)

    plaintext = keys.generate_key()
    row = ApiKey(
        id=ids.new_key_id(),
        tenant_id=principal.tenant_id,
        user_id=user.id,
        secret_hash=keys.hash_key(plaintext),
    )
    db.add(row)
    # The key id, not the user id: the key is the thing created, and two keys
    # minted for the same user must not produce indistinguishable rows. The
    # key id is not sensitive (the secret hash is what's protected).
    audit.record(db, principal, "key.create", row.id, on_behalf_of=on_behalf_of)
    db.commit()
    # The only time the plaintext exists outside the caller's hands (§5.3).
    return CreateKeyResponse(key_id=row.id, key=plaintext)


@router.get("", response_model=ListUsersResponse)
def list_users(
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> ListUsersResponse:
    """SPEC §16.3. Tenant-scoped, like every read in this service."""
    rows = (
        db.query(User)
        .filter(User.tenant_id == principal.tenant_id)
        .order_by(User.created_at)
        .all()
    )
    return ListUsersResponse(
        users=[UserSummary(user_id=r.id, created_at=r.created_at.isoformat()) for r in rows]
    )


@router.get("/{user_id}/keys", response_model=ListKeysResponse)
def list_keys(
    user_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> ListKeysResponse:
    """SPEC §16.3. Revoked keys stay listed: an operator auditing a leak needs
    to see that the revocation happened, not find the row gone."""
    reject_control_characters(user_id, UserNotFound)
    user = db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        raise UserNotFound(user_id=user_id)
    rows = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user.id, ApiKey.tenant_id == principal.tenant_id)
        .order_by(ApiKey.created_at)
        .all()
    )
    return ListKeysResponse(
        keys=[
            KeySummary(key_id=r.id, status=r.status, created_at=r.created_at.isoformat())
            for r in rows
        ]
    )


@router.delete("/{user_id}/keys/{key_id}", status_code=204)
def revoke_key(
    user_id: str,
    key_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> None:
    """SPEC §5.3, §16.3.

    Status flip, not a DELETE: the audit trail must keep showing the key
    existed and when it stopped working. `principal` resolution already
    refuses a non-active key, so this takes effect on the next request with
    no cache to invalidate.

    Filtered on user_id AND tenant AND active: revoking an already-revoked key
    is a 404 rather than a silent success on a second call. There is no row
    lock (`.with_for_update()`), so this is sequential-only: two concurrent
    revokes both read the row as active, both commit, and both return 204 --
    two `key.revoke` audit events for one key, not a 404 for the loser.

    User existence is checked first, like its three siblings (get_user,
    create_key, list_keys): a typoed user id must not read as KEY_NOT_FOUND
    ("already gone"), which would tell an operator killing a leaked key that
    the job is done while the key is still live under the real user.
    """
    reject_control_characters(user_id, UserNotFound)
    reject_control_characters(key_id, KeyNotFound)
    user = db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        raise UserNotFound(user_id=user_id)
    row = (
        db.query(ApiKey)
        .filter(
            ApiKey.id == key_id,
            ApiKey.user_id == user_id,
            ApiKey.tenant_id == principal.tenant_id,
            ApiKey.status == "active",
        )
        .one_or_none()
    )
    if row is None:
        raise KeyNotFound("no such active key for that user", key_id=key_id)
    row.status = "revoked"
    audit.record(db, principal, "key.revoke", row.id, on_behalf_of=on_behalf_of)
    db.commit()
