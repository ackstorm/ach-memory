"""Drop the local credential and group-membership tables.

Identity is entirely external from here: the service mints no credential,
stores none, and reads membership from whatever the caller's identity
provider asserts on each request.

UNRECOVERABLE, and the forward direction is the dangerous one. `api_keys`
holds a SHA-256 of each key and nothing else, so a dropped row cannot be
reconstructed from any backup of this schema alone, and every key it held
stops working the moment this runs -- every CLI install, both plugin hooks
and any service using a `mem_` key fail authentication at deploy, not later.
Callers must already be carrying a token from the configured JWT issuer or
platform resolver before this is applied.

`group_members` is the same shape: the rows recorded membership this service
no longer consults, so restoring them would restore nothing. Membership has
to exist in the IdP.

`users` and `groups` deliberately survive. `User.bank_id` is what makes a
person's memory exist (SPEC §19.2) and `link_identity` keeps populating it;
`groups` is a derived projection that `projects.owner_id` points at.

Revision ID: d1e2f3a4b5c6
Revises: 918c1a7a37de
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: str | Sequence[str] | None = "73e00e0dd20c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS api_keys")
    op.execute("DROP TABLE IF EXISTS group_members")


def downgrade() -> None:
    # Empty compatibility tables only. The keys and the memberships are gone
    # and nothing here can invent them -- a downgrade restores the shape so
    # an older release starts, not the data so it works.
    op.execute(
        "CREATE TABLE IF NOT EXISTS api_keys ("
        "id varchar(64) PRIMARY KEY, "
        "tenant_id varchar(64) NOT NULL REFERENCES tenants(id), "
        "user_id varchar(128) NOT NULL REFERENCES users(id), "
        "secret_hash varchar(64) NOT NULL UNIQUE, "
        "status varchar(16) NOT NULL, "
        "created_at timestamptz NOT NULL)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_api_keys_tenant_id ON api_keys (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_api_keys_user_id ON api_keys (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_api_keys_secret_hash ON api_keys (secret_hash)")
    op.execute(
        "CREATE TABLE IF NOT EXISTS group_members ("
        "group_id varchar(128) NOT NULL REFERENCES groups(id), "
        "user_id varchar(128) NOT NULL REFERENCES users(id), "
        "PRIMARY KEY (group_id, user_id))"
    )
