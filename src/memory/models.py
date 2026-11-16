from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    # No bank_id column: a user's bank is `user_<id>`, derived in `resolve_bank`.
    # Only a project needs the mapping stored, its bank id being a random uuid4.
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExternalIdentity(Base):
    """(issuer, subject) is the only globally unique name an IdP gives us."""

    __tablename__ = "external_identities"

    issuer: Mapped[str] = mapped_column(String(256), primary_key=True)
    subject: Mapped[str] = mapped_column(String(256), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Group(Base):
    """A derived projection of a group an identity provider asserted, never
    the authority on who belongs to it -- membership is read from the
    credential on every request."""

    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"

    # Internal. Public identity lives in ProjectSlug.
    internal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(8))
    owner_id: Mapped[str] = mapped_column(String(128))
    bank_id: Mapped[str] = mapped_column(String(64), unique=True)
    # The closed family list a project retain must pick from; None until one is generated or set.
    families: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    slug_rows: Mapped[list["ProjectSlug"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectSlug(Base):
    __tablename__ = "project_slugs"
    __table_args__ = (
        Index(
            "uq_project_slugs_canonical_project",
            "project_internal_id",
            unique=True,
            postgresql_where=text("is_canonical"),
        ),
    )

    slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_internal_id: Mapped[str] = mapped_column(ForeignKey("projects.internal_id"))
    is_canonical: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    project: Mapped[Project] = relationship(back_populates="slug_rows")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    on_behalf_of: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource: Mapped[str] = mapped_column(String(512))
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        # server_default only -- see db.py's replicas note: one clock
        # (the database's) is what makes listing by created_at mean anything.
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )


class Memory(Base):
    """What ACH owns about one memory: its title, family and pin, plus the state and size the
    index and the pin cap need without asking the engine. The text stays in the engine."""

    __tablename__ = "memories"
    __table_args__ = (Index("ix_memories_owner_state", "owner_id", "state"),)

    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(8))
    # The project's internal id or the user's id, as in the journal's `owner`.
    owner_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(80), nullable=True)
    family: Mapped[str | None] = mapped_column(String(48), nullable=True)
    pinned: Mapped[bool] = mapped_column(default=False)
    memory_type: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(8), default="valid")
    chars: Mapped[int] = mapped_column(Integer)
    # clock_timestamp, not now(): rows written in one transaction (the rollout) still order.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )


class ReadEvent(Base):
    """One read: session-start load, recall, list or get. How we know whether agents use memory."""

    __tablename__ = "read_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    project_internal_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tool: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str | None] = mapped_column(String(8), nullable=True)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    family: Mapped[str | None] = mapped_column(String(48), nullable=True)
    results: Mapped[int] = mapped_column(Integer)
    #: A recall's best decision-model relevance; None when it was not scored.
    relevance: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
