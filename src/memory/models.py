from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
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
    # Orientation, not memory: name, spec pointer and one-line purpose are
    # derivable facts about a project, so they are a record here rather than
    # three Hindsight facts competing for a profile item budget. purpose is
    # capped at 256 because it is one line -- a budget, not a text field.
    # All nullable: every existing project has none of them, and a NOT NULL
    # with a default would invent orientation nobody wrote.
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    canonical_spec: Mapped[str | None] = mapped_column(String(512), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(256), nullable=True)
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


class ActivityEvent(Base):
    """One row per data-plane call. Operational telemetry, NOT the audit
    trail -- these rows age out (memory.activity._prune), while audit_events
    do not.

    No content column, ever. A copy of memory content here would survive
    `DELETE /v1/admin/memory/{scope}`, so SPEC §12.3's only complete erasure
    path would quietly stop being complete. Bytes and a document id say
    enough for "did the write land"; the content itself is read live from
    Hindsight by whoever is authorized to read that bank.
    """

    __tablename__ = "activity_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    # NULL for the master key, exactly as AuditEvent.actor_key_id.
    credential_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    surface: Mapped[str] = mapped_column(String(4))
    scope: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The RESOLVED slug, never the caller's raw argument -- a rename
    # tombstone must not make one project look like two.
    project_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # sha256(bank_id)[:12]. The bank id itself may not leave this service
    # (SPEC inv. 29), and this is enough to correlate a row with Hindsight's
    # own logs without being reversible.
    bank_fingerprint: Mapped[str] = mapped_column(String(16))
    document_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    content_bytes: Mapped[int | None] = mapped_column(nullable=True)
    # Client-declared and unverified: it is a label that tells two agents
    # apart when they share one credential, never attribution.
    agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(8))
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_ms: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        # server_default only, no `default=utcnow` -- see AuditEvent.created_at
        # for why having both silently defeats the server default.
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )


class ContextRevision(Base):
    """One monotonic revision per (user, project, workspace) compiled-context
    snapshot.

    Bumped when any compiler input changes -- a profile refresh, a project
    metadata edit, or a Working State write. Both tiers compiled from the same
    snapshot carry the same value; a cached tier keeps the value it was
    compiled at, which is what makes "INDEX rev 42 / FULL rev 39" readable
    without reconciliation logic in the agent.

    Per (user, project) and not per project: half the snapshot is that user's
    own profile, so a shared counter would bump for a colleague's refresh and
    every consumer would re-read a brief nothing had changed.

    Per workspace because Working State is: two git worktrees of the same
    project hold different state, so a change in one must not bump the
    revision the other is holding a cache against.
    """

    __tablename__ = "context_revisions"

    # Plain columns, no foreign keys: project_slug is "" for the snapshot with
    # no project, which no projects row can satisfy, and a composite key half
    # constrained is worse than one that is uniformly derived state.
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # "" rather than NULL: this is a primary key, and NULL never equals NULL.
    project_slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    # "" for a snapshot with no workspace, same reasoning as project_slug.
    # A Python-side default (not server_default, deliberately dropped in the
    # migration): revisions.current() does not thread workspace_id through
    # until the brief compiler is made workspace-aware, and every existing
    # caller must keep inserting the no-workspace row unchanged until then.
    workspace_id: Mapped[str] = mapped_column(String(35), primary_key=True, default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # sha256 of the compiler inputs; 64 hex characters.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkingSession(Base):
    """A session's ordering identity within one (user, project, workspace)
    scope -- never its content.

    `session_epoch` is server-issued and strictly increasing by creation
    order (GENERATED BY DEFAULT AS IDENTITY), so no caller can inflate one by
    inventing a large value and none ever repeats. The unique constraint below
    is what makes `working_state.start_session` idempotent: reusing the same
    session_id in the same scope must resolve to the row already there, never
    allocate a second identity value.
    """

    __tablename__ = "working_sessions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "project_internal_id",
            "workspace_id",
            "session_id",
            name="uq_working_sessions_scope_session",
        ),
    )

    session_epoch: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    project_internal_id: Mapped[str] = mapped_column(ForeignKey("projects.internal_id"))
    workspace_id: Mapped[str] = mapped_column(String(35))
    session_id: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkingState(Base):
    """The one live explicit handoff per (tenant, user, project, workspace).

    PostgreSQL state, never a Hindsight fact, observation, document or
    mental-model input. Replacement is total: no payload history, no TTL, no
    decay -- `working_state.replace()` overwrites every field at once and
    only for a lexicographically greater (session_epoch, checkpoint_seq).
    """

    __tablename__ = "working_states"
    __table_args__ = (
        CheckConstraint("session_epoch >= 0", name="ck_working_states_session_epoch_non_negative"),
        CheckConstraint("checkpoint_seq >= 0", name="ck_working_states_checkpoint_seq_non_negative"),
    )

    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    project_internal_id: Mapped[str] = mapped_column(
        ForeignKey("projects.internal_id"), primary_key=True
    )
    workspace_id: Mapped[str] = mapped_column(String(35), primary_key=True)

    objective: Mapped[str] = mapped_column(Text)
    current_direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    recent_decisions: Mapped[list] = mapped_column(JSON)
    open_questions: Mapped[list] = mapped_column(JSON)
    next_steps: Mapped[list] = mapped_column(JSON)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The session/ordering pair this payload was written by -- not a
    # foreign key to working_sessions.session_epoch: the session row can
    # never be deleted while this references it, and the pair is compared
    # lexicographically, never joined.
    session_id: Mapped[str] = mapped_column(String(128))
    session_epoch: Mapped[int] = mapped_column(BigInteger)
    checkpoint_seq: Mapped[int] = mapped_column(BigInteger)
