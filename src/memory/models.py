from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
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
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
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
        DateTime(timezone=True), default=utcnow, index=True
    )
