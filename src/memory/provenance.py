from memory.errors import InvalidMetadata

# SPEC §13.4, "reserved at minimum". A client may not set any of these: the
# server is authoritative for them, and a memory that lies about who wrote it
# or which project it belongs to is worse than no provenance at all.
RESERVED_KEYS = frozenset(
    {
        "tenant_id",
        "user_id",
        "project_slug",
        "memory_key",
        "on_behalf_of",
        "agent",
        "client_name",
    }
)

# Reserved keys the server enforces against client overwrite. `agent` and
# `client_name` are deliberately excluded: they are reserved (RESERVED_KEYS)
# so a client cannot clobber an authoritative value, but the server has no
# authoritative value of its own for them in v1 — the client's is the only
# value there is, so it is let through and kept.
_SERVER_OWNED = RESERVED_KEYS - {"agent", "client_name"}

# SPEC §13.2 splits client-supplied runtime fields two ways: the extraction
# six (agent, source, git_branch, git_commit, pr, workspace) go to Hindsight;
# everything else the runtime knows (os, arch, client_version, client_name,
# plus tenant_id/authenticated_user/on_behalf_of, which never arrive through
# client_metadata at all) is audit/runtime context that must never reach
# Hindsight's extraction input. v1 has no consumer for that context beyond
# "don't extract on it" -- nothing in this service persists or logs it -- so
# build() below returns only the extraction mapping. It used to return a
# second "audit" mapping too; every caller discarded it (`_audit`), and nothing
# else in the codebase called build() at all, so it was computed and thrown
# away on every retain. Collapsed rather than wired to a store that does not
# exist, per YAGNI -- add a second return value back if v1 ever gains one.
#
# `client_name` is not one of §13.2's extraction six, and it is
# client-reported identity like `client_version`, so this module reads it as
# audit-only alongside `os`/`arch`/`client_version`. §13.2 does not place
# `client_name` on either side explicitly — this is our reading, not a
# quotation of the spec.
AUDIT_ONLY_KEYS = frozenset({"os", "arch", "client_version", "client_name"})


def build(
    client_metadata: dict[str, str] | None,
    *,
    project_slug: str | None,
) -> dict[str, str]:
    """Extraction metadata Hindsight sees (SPEC §13.2).

    Raises InvalidMetadata before anything is written if the client tried to
    set a server-owned key.

    Precedence (SPEC §13.3): this function's inputs are the authoritative
    runtime/server layer and always win. `MEMORY_PROJECT_METADATA` project
    metadata is merged UNDERNEATH them — merge project metadata first, then
    let runtime/server fields overwrite on key collision — but that merge is
    the MCP CLIENT's job, the same way §10's MEMORY_PROJECT / Git-locator
    derivation is: an agent harness reads its own environment and builds the
    `metadata` argument BEFORE calling a tool, this service never sees
    `MEMORY_PROJECT_METADATA` and performs no such merge itself.

    (Plan 3's progress ledger deferred this merge to "the MCP task" expecting
    it to land here once the MCP surface existed; Plan 4 shipped that surface
    without it, on purpose, once it was clear the merge has no wrapper-side
    home to land in. The ledger's "silently not done" is resolved as
    out-of-scope-by-design, not a missed task — see
    `.superpowers/sdd/progress.md`, Plan 4 final review.)

    This function owns only the one flat mapping it is given as
    `client_metadata`, already merged by whoever called it.

    One more caller-visible detail: an empty-string `project_slug` is treated
    as absent (no `project_slug` key in the returned mapping).
    """
    supplied = dict(client_metadata or {})

    for key in supplied:
        if key.strip().lower() in _SERVER_OWNED:
            # Checked over the whole submitted mapping before any merging,
            # so §13.4's "nothing is written" holds for the whole request,
            # not just for the offending key. Matched on the normalized form
            # so a near-miss spelling (`User_Id`, `" user_id"`) can't slip a
            # reserved-looking key into extraction; the original spelling is
            # still reported back to the client.
            raise InvalidMetadata(
                "that metadata key is reserved by the server", key=key
            )

    # Normalized the same way the reserved-key check above is: an unnormalized
    # membership test here let `OS`/`ARCH`/`Client_Name`/`Client_Version`
    # straight through to extraction (measured live) while the lowercase
    # spelling was correctly held back -- the two checks must agree on what
    # "the same key" means, or a case variant slips past whichever one forgot.
    extraction = {
        k: v for k, v in supplied.items() if k.strip().lower() not in AUDIT_ONLY_KEYS
    }

    if project_slug:
        extraction["project_slug"] = project_slug

    return extraction


def context_line(metadata: dict[str, str]) -> str | None:
    """Hindsight's short free-text context field (SPEC §13.5).

    "interactive-coding via codex on feature/auth". Built only from parts that
    are present, and None when there is nothing to say — an empty or
    half-formed sentence is worse than no context.
    """
    source = metadata.get("source")
    agent = metadata.get("agent")
    branch = metadata.get("git_branch")

    parts = [source or agent]
    if source and agent:
        parts.append(f"via {agent}")
    if branch:
        parts.append(f"on {branch}")

    if not parts[0]:
        return None
    return " ".join(p for p in parts if p)
