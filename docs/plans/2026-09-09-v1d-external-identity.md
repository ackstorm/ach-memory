# v1d — External Identity Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove every internal identity concept from ach-memory. Users, groups and operator authority all come from outside; the service stores no credential and mints none.

**Architecture:** Identity keeps the two shapes that already exist — a forwarded header resolved at an endpoint, and a verifiable JWT. What goes is everything local: minted API keys, the `mem_` prefix, the user and group management routes, their tables, and the master key. Operator authority stops being a shared secret and becomes configuration evaluated over an already-resolved external identity.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, FastAPI, pytest, ruff, uv, Docker Compose.

**Testing discipline:** every task names ONE test file. Run only that file. `make test` and the full suite are FORBIDDEN until the final task.

**Order:** this plan runs LAST. v1b changes provisioning rules that touch the same routes, and v1c is independent. Landing this first would force both to be rewritten against a moving auth surface.

---

## Decisions this plan implements

1. **Two external providers stay:** `auth/providers/platform.py` (header forward) and `auth/providers/jwt_provider.py` (verifiable token). The "sidecar" is a **deployment artifact** that speaks the platform shape when no LiteLLM or ACH sits in front — not a third code path. Do not add one.
2. **Operator authority is configuration:** `MEMORY_MASTER_GROUPS` and `MEMORY_MASTER_USERS`, evaluated over the resolved principal.
3. **Both default to empty, provably.** A startup assertion, not merely an empty default.
4. **`User` stays.** It is the bank anchor, not an identity record.

---

## What the master key actually is

The premise that it is an ACH workaround is wrong, and an executor who believes it will delete the operator plane. It is 49 references across 19 files, and it gates:

| Route | Purpose |
|---|---|
| `GET /v1/admin/audit` | the audit log |
| `POST /v1/admin/memory/{scope}/clear` | empty a bank |
| `DELETE /v1/admin/memory/{scope}` | delete a bank |
| `POST /v1/admin/slugs/{slug}/release` | release a retired slug |
| `GET /v1/activity`, `/summary` | fleet view |
| `on_behalf_of` header | delegation provenance |

All of it survives. Only the credential changes.

---

## The semantic fork an executor will trip on

`is_master` today means **authority without identity**, written into three places:

- `banks.py:20` — *"A master key has no identity of its own, so it must name its target"*
- `projects.py:184` — *"A master key has no identity, so there is no owner to assign"*
- `api/projects.py:138` — *"a master-key create must name an owner"*

An operator under the new model **is** an external user with a bank who also has authority. The two concepts separate, and all three exceptions **disappear** rather than being ported: the operator reaches their own bank like anyone else, creates projects they own like anyone else, and naming someone else's target becomes the authority part rather than a requirement.

Do not mechanically translate these branches. Delete them and let the ordinary path handle an operator.

---

## Task 1: Operator authority from configuration

**Files:**
- Modify: `src/memory/config.py`, `src/memory/auth/principal.py`
- Test: `tests/test_auth.py` — confirm the filename first

**Step 1: Write the failing test**

```python
def test_an_unset_master_config_grants_nobody():
    """The failure mode that matters is not 'the wrong person is an operator',
    it is 'an unset variable made everyone one'. An empty default is not
    enough on its own -- an empty string splits into [''] and would match a
    principal whose group id is the empty string."""
    settings = Settings(master_users="", master_groups="")
    assert not is_operator(Principal(user_id="", groups=frozenset({""})), settings)

def test_a_configured_group_grants_operator():
    ...
def test_a_configured_user_grants_operator():
    ...
```

**Step 3: Implement**

Parse both settings into frozensets, discarding empty strings. `is_master` becomes a derived property of the resolved principal, not a field set by a credential path.

**Step 5: Commit**

```bash
git commit -m "feat(auth): derive operator authority from configuration"
```

---

## Task 2: Startup assertion

**Files:**
- Modify: `src/memory/api/app.py`
- Test: same file as Task 1

The service must refuse to start if the master configuration could grant from an unset or malformed value. Fail closed and loudly — a silent misconfiguration here hands every bank to everyone.

```bash
git commit -m "feat(auth): refuse to start on a master config that could over-grant"
```

---

## Task 3: Delete the local credential path

**Files:**
- Delete: `src/memory/auth/providers/local_key.py`, `src/memory/auth/keys.py`
- Modify: `src/memory/auth/principal.py:60-102`
- Modify: `src/memory/ratelimit.py` (remove `MASTER_KEY_ID`)
- Test: `tests/test_auth.py`

The `mem_` prefix (`principal.py:78`) exists to discriminate a local key from a JWT on `Authorization`. With no local keys there is nothing to discriminate, so the branch goes with it.

Worth knowing while removing it, in case the deletion is ever reconsidered: `local_key.authenticate` never checked the prefix — only the dispatcher did, and only on the Bearer branch. So the same prefix-less master key worked through `x-ach-key` and was refused through `Authorization: Bearer`.

Rewrite the "no identity provider accepts this credential" message: it will be the only refusal left.

```bash
git commit -m "refactor(auth): remove the local API key credential path"
```

---

## Task 4: Delete the user and group management routes

**Files:**
- Delete: `src/memory/api/users.py`, `src/memory/api/groups.py`
- Modify: `src/memory/api/app.py` (router registration)
- Modify: `src/memory/projects.py:127-135` (`authorize`)
- Test: `tests/test_projects.py`

**A behaviour change to name, not a no-op:** `authorize` today grants on an IdP assertion **or** a local row — `projects.py:132` is `or db.get(GroupMember, (project.owner_id, principal.user_id))`, deliberately checked independently and never merged. Dropping `group_members` makes access IdP-only. Write a test that pins the new rule rather than letting it change silently.

`_validate_owner` (`projects.py:79-90`) creates a `Group` row on demand from an IdP assertion. Decide whether `Group` survives as a row at all, or whether ownership stores the asserted id directly. If the table goes, that branch goes with it.

```bash
git commit -m "refactor(api): remove user and group management"
```

---

## Task 5: Migration

**Files:**
- Create: a new Alembic revision
- Test: `tests/test_migrations.py` — confirm the filename first

Drops `api_keys`, and `groups`/`group_members` if Task 4 retired them. `users` **stays** — it is the bank anchor; `link_identity` populates it and `User.bank_id` is what makes memory exist.

Follow the repo's existing rule that a migration must be safe forward and must never be edited once applied. State in the revision docstring what is unrecoverable: a dropped `api_keys` row cannot be reconstructed, and every key it held stops working at deploy.

```bash
git commit -m "feat(db): drop the local credential and group tables"
```

---

## Task 6: Migrate the shipped clients

**This is the largest practical risk in the plan and it is not server-side.**

- `src/memory/cli.py:82` sends `Authorization: Bearer {api_key}`
- `src/memory/cli.py:356`, `:380` write that form into host configuration
- `plugins/claude-code/.mcp.json:15` and both `session-start.sh` hooks gate on `ACH_MEMORY_API_KEY`

`Authorization: Bearer` still works — a JWT rides the same header — so `ACH_MEMORY_API_KEY` becomes "whatever token your identity provider issues" rather than a minted `mem_` key. It is a credential and documentation migration, not a client rewrite. But **every existing install breaks at upgrade**, so this task is where that is handled, not discovered.

**Files:**
- Modify: `src/memory/cli.py`, `README.md`, `.env.example`, both session-start scripts
- Test: `tests/test_cli.py` — confirm the filename first

The preflight (`cli.py:843`) should give a caller a usable message when their token is no longer accepted, naming the provider rather than the prefix.

```bash
git commit -m "feat(cli): carry an externally issued token instead of a minted key"
```

---

## Task 7: A sidecar for the compose stack

With no local credential, the stack cannot be driven without an identity source. The sidecar is a container that answers the identity JSON — **no `if dev:` branch in the auth code**, which is the whole point of choosing this over a development provider.

**Files:**
- Modify: `docker-compose.yml`, `.env.example`, `README.md`
- Create: whatever minimal service answers the whoami shape

Read `auth/providers/platform.py` for the exact response shape and the settings that name its fields before writing it.

```bash
git commit -m "feat(compose): add an identity sidecar so the local stack is drivable"
```

---

## Task 8: Retire `POST /v1/bootstrap`

By now v1b has made `retain` create and provision, and this plan has removed the only caller that needed a master key to bootstrap somebody else. Check whether anything still needs it — `mcp/proxy.py:83-95` pre-warms with it — and either keep it as an explicit pre-warm or remove it. Report which, with the reason.

```bash
git commit -m "refactor(bootstrap): reduce bootstrap to a pre-warm"
```

---

## Task 9: Full sweep and release

```bash
make lint && make test && make secrets && make chart
```

Expect substantial work in every test that minted a key or used `master_headers`. Those fixtures are the largest single body of change in the suite; budget for it.

Then the release, which is a **major**: the surface breaks, three tables go, and every existing credential stops working.

```bash
make release-bump VERSION=1.0.0
git commit -am "chore(release): bump release metadata to 1.0.0"
make release-cut VERSION=1.0.0
```

---

## Out of scope

- **A third auth mechanism.** The sidecar speaks the existing platform shape. If you find yourself adding a provider module, stop: the design says there is no new code path.
- **A permission model.** Operator or not, nothing between. `transfer`'s accepted consequence (SPEC §6.1) stands: a group member can take a group project private, and the audit event is the mitigation.
- **Multi-tenancy changes.** `link_identity` already refuses to re-point an identity at another tenant.
