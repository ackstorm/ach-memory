from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)


class User(Base):
    __tablename__ = "users"

    # Externally supplied (ACH) or service-generated. SPEC §4.2.
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    # Allocated here; materialized in Hindsight on first use. SPEC §19.2.
    bank_id: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    # NOT NULL on purpose: every row here is a user key. The bootstrap master
    # key is configuration, never a row (SPEC §5.2), so a user-less row is not
    # a legitimate state — and if one existed, principal resolution must never
    # be able to read it as "this key is the master key".
    # index=True: GET /v1/users/{id}/keys filters on exactly this column, and
    # every other tenant-scoped FK in this schema already carries one.
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ExternalIdentity(Base):
    __tablename__ = "external_identities"

    # (issuer, subject) is the only globally unique name an IdP gives us, and
    # neither half works alone as a User.id. ACH's `sub` is a bare owner email
    # (ach/internal/forwarder/jwt/signer.go); Dex's is an opaque identifier;
    # a master-provisioned user is `usr_<uuid>` from ids.py. Keying on the
    # subject alone would collapse the same string from two issuers into one
    # person, and keying on nothing would mint a second User -- and a second
    # bank_id -- on every request, silently splitting one human's memory in
    # half with no error anywhere.
    issuer: Mapped[str] = mapped_column(String(256), primary_key=True)
    subject: Mapped[str] = mapped_column(String(256), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # Width matches AuditEvent.actor_key_id, which stores this value for an
    # external caller: that column now holds either an `key_`-prefixed
    # api_keys.id or an `ext_`-prefixed credential from here, and this row is
    # what resolves the latter back to a human. The prefixes are disjoint by
    # construction (ids.py), so a reader can always tell which namespace it
    # is looking at.
    credential_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Group(Base):
    __tablename__ = "groups"

    # Externally supplied (ACH) or service-generated, like User. SPEC §4.3.
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class GroupMember(Base):
    __tablename__ = "group_members"

    # No roles inside a group in v1 (SPEC §4.3): membership is the whole model.
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("tenant_id", "project_slug"),)

    # Internal. The public identity is project_slug (SPEC inv. 7).
    internal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    project_slug: Mapped[str] = mapped_column(String(128), index=True)
    # Metadata, never identity and never authorization evidence (inv. 11).
    # Deliberately NOT unique: SPEC §17.
    git_locator: Mapped[str | None] = mapped_column(String(512), nullable=True)
    owner_type: Mapped[str] = mapped_column(String(8))
    owner_id: Mapped[str] = mapped_column(String(128))
    bank_id: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RetiredSlug(Base):
    __tablename__ = "retired_slugs"

    # A forwarding tombstone (SPEC §8.6). Resolution follows it in ONE hop:
    # a rename mutates the slug on the same Project row, so internal_id never
    # changes — every tombstone already points at the row, so there is never
    # a chain to walk and never a cycle.
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    retired_slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_internal_id: Mapped[str] = mapped_column(
        ForeignKey("projects.internal_id")
    )
    retired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    # NULL for the bootstrap master key, which is configuration not a row.
    actor_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    on_behalf_of: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    # 512, not 256: several call sites compose this from two already-bounded
    # externally-supplied ids (User.id/Group.id are each String(128)), e.g.
    # group.add_member's f"{group_id}/{user_id}" (<=257) and
    # project.transfer's f"{slug}: {owner_type}:{owner_id} -> ..." (<=408
    # worst case). Matches Project.git_locator's bound -- the existing
    # "wide" column in this schema -- rather than inventing a new number, and
    # comfortably clears the computed worst case without truncating (a
    # truncated audit record is its own kind of wrong).
    resource: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        # server_default ONLY -- no `default=utcnow`. The Python-side default
        # stamps each replica's own clock on the row, and the Helm chart
        # exposes replicaCount. One clock -- the database's -- is what makes
        # list_audit's ordering mean anything; `admin.list_audit` already
        # concedes its id tiebreak "buys determinism, not recency". A
        # `default` and a `server_default` on the same column both being set
        # is not "belt and suspenders": SQLAlchemy always prefers the
        # Python-side `default` when both are present, so `default=utcnow`
        # here silently defeated server_default's whole point -- every insert
        # still carried the column in its VALUES list with a bound
        # `created_at` parameter, and the DDL default never fired. Verified
        # against the compiled INSERT (test_models.py).
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
