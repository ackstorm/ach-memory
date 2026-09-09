import uuid as uuid_module
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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

    # Internal. Public identity lives in ProjectSlug (SPEC inv. 7).
    internal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    # Metadata, never identity and never authorization evidence (inv. 11).
    # Deliberately NOT unique: SPEC §17.
    git_locator: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Orientation, not memory: name, spec pointer and one-line purpose are
    # derivable facts about a project, so they are a record here rather than
    # three Hindsight facts competing for standing-context budget. purpose is
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
    slug_rows: Mapped[list["ProjectSlug"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectSlug(Base):
    __tablename__ = "project_slugs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_project_slugs_tenant_slug"),
        Index(
            "uq_project_slugs_canonical_project",
            "tenant_id",
            "project_internal_id",
            unique=True,
            postgresql_where=text("is_canonical"),
        ),
    )

    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_internal_id: Mapped[str] = mapped_column(
        ForeignKey("projects.internal_id")
    )
    is_canonical: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    project: Mapped[Project] = relationship(back_populates="slug_rows")


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
    completed_checkpoint_seq: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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




class RetainedRecord(Base):
    """ACH's durable provenance row for one exact typed retain."""

    __tablename__ = "retained_records"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'user' AND user_id IS NOT NULL AND project_internal_id IS NULL) "
            "OR (scope = 'project' AND user_id IS NULL AND project_internal_id IS NOT NULL)",
            name="ck_retained_records_scope_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            "operation_id",
            name="uq_retained_records_bank_operation",
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint(
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            "document_id",
            name="uq_retained_records_bank_document",
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "ix_retained_records_due",
            "tenant_id",
            "scope",
            "valid_until",
        ),
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_module.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    scope: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    project_internal_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.internal_id"), nullable=True
    )
    operation_id: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    document_id: Mapped[str] = mapped_column(String(128))
    source_memory_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    canonical_content: Mapped[str] = mapped_column(Text)
    memory_type: Mapped[str] = mapped_column(String(16))
    basis: Mapped[str] = mapped_column(String(32))
    trigger: Mapped[str] = mapped_column(String(32))
    sanitized_evidence: Mapped[list[dict[str, str | None]]] = mapped_column(JSON)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lifecycle: Mapped[str] = mapped_column(String(16))
    upstream_state: Mapped[str] = mapped_column(String(16))
    calling_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_credential: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )


class RetainedRecordRevision(Base):
    """One immutable prior canonical value a correction overwrote.

    Never updated once written -- `retained_records.append_correction_revision`
    only ever inserts, keyed by the correction's own deterministic
    `curation_operation_id` so an exact retry returns the existing row instead
    of appending a second one. `ON DELETE CASCADE` ties a claim's whole
    correction history to its own hard delete (SPEC §12.2): no orphaned
    revision can outlive the claim it revised.
    """

    __tablename__ = "retained_record_revisions"
    __table_args__ = (
        UniqueConstraint(
            "retained_record_id",
            "revision",
            name="uq_retained_record_revisions_record_revision",
        ),
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_module.uuid4
    )
    retained_record_id: Mapped[uuid_module.UUID] = mapped_column(
        ForeignKey("retained_records.id", ondelete="CASCADE")
    )
    curation_operation_id: Mapped[str] = mapped_column(String(128), unique=True)
    revision: Mapped[int] = mapped_column(Integer)
    canonical_content: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64))
    memory_type: Mapped[str] = mapped_column(String(16))
    basis: Mapped[str] = mapped_column(String(32))
    trigger: Mapped[str] = mapped_column(String(32))
    sanitized_evidence: Mapped[list[dict[str, str | None]]] = mapped_column(JSON)
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CurationOperation(Base):
    """A durable desired curation outcome and its upstream proof state."""

    __tablename__ = "curation_operations"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'user' AND user_id IS NOT NULL AND project_internal_id IS NULL) "
            "OR (scope = 'project' AND user_id IS NULL AND project_internal_id IS NOT NULL)",
            name="ck_curation_operations_scope_identity",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    retained_record_id: Mapped[uuid_module.UUID] = mapped_column(
        ForeignKey("retained_records.id")
    )
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    scope: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    project_internal_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.internal_id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(16))
    desired_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(32))
    repair_not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BankCurrentness(Base):
    """The one current-read safety barrier for a logical bank."""

    __tablename__ = "bank_currentness"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'user' AND user_id IS NOT NULL AND project_internal_id IS NULL) "
            "OR (scope = 'project' AND user_id IS NULL AND project_internal_id IS NOT NULL)",
            name="ck_bank_currentness_scope_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            name="uq_bank_currentness_bank",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_module.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    scope: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    project_internal_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.internal_id"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(16))
    blocking_operation_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    repair_not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MentalModelRegistration(Base):
    """ACH-owned logical identity and lifecycle for one upstream model."""

    __tablename__ = "mental_model_registrations"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'user' AND user_id IS NOT NULL AND project_internal_id IS NULL) "
            "OR (scope = 'project' AND user_id IS NULL AND project_internal_id IS NOT NULL)",
            name="ck_mental_model_registrations_scope_identity",
        ),
        CheckConstraint(
            "(origin = 'builtin' AND builtin_key IS NOT NULL "
            "AND definition_version IS NOT NULL) "
            "OR (origin = 'user' AND builtin_key IS NULL "
            "AND definition_version IS NULL)",
            name="ck_mental_model_registrations_origin_metadata",
        ),
        CheckConstraint(
            "max_tokens > 0", name="ck_mental_model_registrations_positive_tokens"
        ),
        UniqueConstraint(
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            "model_key",
            name="uq_mental_model_registrations_bank_key",
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "uq_mental_model_registrations_bank_upstream",
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            "upstream_model_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("upstream_model_id IS NOT NULL"),
        ),
        Index(
            "uq_mental_model_registrations_bank_builtin",
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            "builtin_key",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("builtin_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_module.uuid4
    )
    model_key: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    scope: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    project_internal_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.internal_id"), nullable=True
    )
    upstream_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(256))
    source_query: Mapped[str] = mapped_column(Text)
    source_tags: Mapped[list[str]] = mapped_column(JSON)
    tags_match: Mapped[str] = mapped_column(String(8))
    max_tokens: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[dict[str, object]] = mapped_column(JSON)
    origin: Mapped[str] = mapped_column(String(16))
    builtin_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    definition_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lifecycle_state: Mapped[str] = mapped_column(String(32))
    mutation_operation_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    mutation_payload_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    delivery_state: Mapped[str] = mapped_column(String(16))
    refresh_operation_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    refresh_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    repair_not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MentalModelMutation(Base):
    """Idempotency ledger for the update/refresh/delete mutations of an
    already-registered mental model.

    `create` has its own ledger already -- `MentalModelRegistration`'s own
    `mutation_operation_id`/`mutation_payload_hash` columns, since a fresh
    `creating` row IS the durable idempotency record for that action. Update/
    refresh/delete act on an EXISTING row that may be mutated many times over
    its life, so each needs its own operation identity kept separately here
    rather than overwriting the row's single create-time pair.
    """

    __tablename__ = "mental_model_mutations"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'user' AND user_id IS NOT NULL AND project_internal_id IS NULL) "
            "OR (scope = 'project' AND user_id IS NULL AND project_internal_id IS NOT NULL)",
            name="ck_mental_model_mutations_scope_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "scope",
            "user_id",
            "project_internal_id",
            "operation_id",
            name="uq_mental_model_mutations_bank_operation",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_module.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    scope: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    project_internal_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.internal_id"), nullable=True
    )
    model_key: Mapped[str] = mapped_column(String(64))
    operation_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(16))
    payload_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    upstream_operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
