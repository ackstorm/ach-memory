# Project sharing

Share a project's memory with people and ACH teams, from the CLI or by asking the agent.

## Rule

A project belongs to the users and groups on its access list; every one of them may do
everything with it (read, write, share, unshare, rename, set families). The creator is the
first user on the list and can be removed like anyone else. Personal memory is never shared.

- `share group` accepts only a group the caller belongs to (one `whoami` shows). `unshare`
  accepts any entry on the list: removing grants nothing.
- An `unshare` that would empty the list is refused: nobody but an operator could reach the
  project again.

## Identity

| Entry | Matched against | Source |
|---|---|---|
| user | `Principal.subject`, case-insensitive | JWT `sub`, the caller's email |
| group | `Principal.groups`, exact | JWT `teams` (`MEMORY_AUTH_JWT_GROUPS_CLAIM`): every ACH team alias of the caller, minted when ach-memory's BackendIdentityPolicy sets `forwardTeams` |

## Storage

`projects.access` (JSON, not null): `{"users": [...], "groups": [...]}`, sorted and deduplicated,
users lower-cased. It replaces `owner_type` and `owner_id`, which are dropped.

Migration `0008_project_access`:
- user-owned project: `users` = the owner's subjects from `external_identities`, lower-cased.
  An owner with no identity row fails the migration, so no project is silently orphaned.
- group-owned project: `groups` = `[owner_id]`.

A new project starts as `{"users": [<creator's subject>], "groups": []}`.

`projects.authorized`: operator, or `subject.lower() in access.users`, or
`groups & access.groups`. Every project operation already goes through it.

## MCP tools

| Tool | Does | Journal |
|---|---|---|
| `whoami()` | `{subject, groups, operator}` | no |
| `list_access(project_slug)` | `{users, groups}` | no |
| `share_project(project_slug, users=[], groups=[])` | adds, returns the list | `project.share` |
| `unshare_project(project_slug, users=[], groups=[])` | removes, returns the list | `project.unshare` |

`share_project` / `unshare_project` descriptions: call them when the user asks to share or stop
sharing the project; never on your own initiative. Entries are validated at the boundary:
non-empty, no control characters, users ≤256 chars, groups ≤128 chars. No `@` check on users:
the subject is whatever the identity provider asserts (an email in production).

## CLI

```
ach-memory whoami
ach-memory share                       # list
ach-memory share user <email>...
ach-memory share group <team>...
ach-memory unshare user <email>...
ach-memory unshare group <team>...
```

`--project` as in `families`; otherwise the project comes from the folder.

## Skill

`skills/ach-memory/references/sharing.md`, named from `SKILL.md` with when to read it ("before
sharing a project or when the user asks who can see it"). It says:

- "Share this project with Joan Pau" → `share_project` with his email, then answer with the
  list it returns ("done: Joan Pau has access, with you and team dream").
- A name without an email: look it up with a contacts tool if the host has one, else ask.
  Never guess an address.
- A team: run `whoami`; the team must be in your groups.
- Whoever is shared with is a co-owner and can remove you; personal memory is never shared.
- The CLI equivalents.

## Tests

`tests/test_sharing.py`: a shared user reads and writes; a shared group reads; a stranger is
refused; a shared user can share, rename and remove the creator; sharing a group the caller is
not in is refused; emptying the list is refused; the user bank stays private; `whoami` returns
subject and groups. `tests/test_projects.py` and `tests/test_models.py` move from
`owner_type`/`owner_id` to `access`.

## Outside this repo

- gitops: `forwardTeams: true` on ach-memory's BackendIdentityPolicy (ACH 1.3.7+) and
  `config.auth.jwt.groupsClaim: teams` in its HelmRelease.
- A team per Google group that has none (e.g. `operaciones`, today folded into `run`):
  `LiteLLMTeam` plus a `teams.sso` entry.
- `scripts/rollout-0.12.py` reads `owner_type`/`owner_id`; it is a finished one-off rollout
  and stops working once they are dropped.
